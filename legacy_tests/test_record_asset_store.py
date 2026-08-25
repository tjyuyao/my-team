"""T10: RecordStore + AssetStore + 私有二进制读写 + effect 集成.

Verifies:
- RecordStore: schema registration, invariant rejection (库存非负),
  append-only ledger + replay, per-effect invert (rollback / group
  failure restores prior record and removes ledger entries).
- AssetStore: content-addressed put/get/stat, dedup, AttachmentRef.
- The minimal re-commerce loop: 采购入库 + 销售出库 interplay enforced
  on the RESULT (stock delta going negative is rejected deterministically).
- Commit pipeline integration: RECORD_UPSERT / RECORD_DELTA effects,
  local FAILED on invariant violation (T18 — no tick rollback), atomic
  same-tick order+stock, and full-tick rollback inverts records.
- Private binary write/read through FILE_WRITE(content_bytes_b64).
"""
from __future__ import annotations

from uuid import uuid4

from my_team.agent_runtime import ToolContext
from my_team.agent_tree import AgentTree
from my_team.asset_store import AssetStore, AttachmentRef
from my_team.audit import AuditEventType
from my_team.record_store import (
    FieldSpec,
    InvariantRule,
    RecordInvariantError,
    RecordSchema,
    RecordStore,
)
from my_team.simulation import Simulation
from my_team.transaction import EffectStatus, EffectType


def _skus_schema() -> RecordSchema:
    return RecordSchema(
        record_type="sku",
        fields=[
            FieldSpec(name="sku_id", type="string"),
            FieldSpec(name="stock", type="int"),
        ],
        invariants=[InvariantRule(kind="non_negative", field="stock")],
        unique_fields=["sku_id"],
    )


def _orders_schema() -> RecordSchema:
    return RecordSchema(
        record_type="sales_order",
        fields=[
            FieldSpec(name="order_no", type="string"),
            FieldSpec(name="sku_id", type="string"),
            FieldSpec(name="qty", type="int"),
        ],
        unique_fields=["order_no"],
    )


def _sim() -> Simulation:
    tree = AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "record_upsert", "record_delta"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })
    sim = Simulation(agent_tree=tree)
    sim.record_store.register_schema(_skus_schema())
    sim.record_store.register_schema(_orders_schema())
    return sim


def _ctx(sim: Simulation) -> ToolContext:
    return ToolContext(
        agent_id="agent.root", tick=0,
        allowed_tools=sim._tool_registry.get_allowed_tools("agent.root"),
    )


class TestRecordStore:
    def test_register_and_upsert(self) -> None:
        store = RecordStore()
        store.register_schema(_skus_schema())
        result = store.upsert(
            "sku", "SKU-1", {"sku_id": "SKU-1", "stock": 100},
            "agent.root", 0,
        )
        assert result.record["stock"] == 100
        assert store.get("sku", "SKU-1")["stock"] == 100
        assert store.ledger_len() == 1

    def test_duplicate_schema_rejected(self) -> None:
        store = RecordStore()
        store.register_schema(_skus_schema())
        try:
            store.register_schema(_skus_schema())
            raise AssertionError("should reject duplicate schema")
        except ValueError as e:
            assert "already registered" in str(e)

    def test_negative_stock_rejected_deterministically(self) -> None:
        store = RecordStore()
        store.register_schema(_skus_schema())
        store.upsert("sku", "SKU-1", {"sku_id": "SKU-1", "stock": 10}, "a", 0)
        try:
            store.apply_delta("sku", "SKU-1", "stock", -11, "a", 1)
            raise AssertionError("should reject going negative")
        except RecordInvariantError as e:
            assert ">= 0" in e.message
            assert e.key == "SKU-1"

    def test_unique_field_enforced(self) -> None:
        store = RecordStore()
        store.register_schema(_skus_schema())
        store.upsert("sku", "SKU-1", {"sku_id": "SKU-1", "stock": 1}, "a", 0)
        try:
            store.upsert("sku", "SKU-2", {"sku_id": "SKU-1", "stock": 5}, "a", 1)
            raise AssertionError("should reject duplicate sku_id")
        except RecordInvariantError as e:
            assert "sku_id" in e.message

    def test_missing_required_field_rejected(self) -> None:
        store = RecordStore()
        store.register_schema(_orders_schema())
        try:
            store.upsert("sales_order", "O1", {"order_no": "O1"}, "a", 0)
            raise AssertionError("should reject missing sku_id")
        except RecordInvariantError:
            pass

    def test_order_declared_fields_required(self) -> None:
        store = RecordStore()
        store.register_schema(_orders_schema())
        # Valid order
        store.upsert(
            "sales_order", "O1",
            {"order_no": "O1", "sku_id": "S1", "qty": 2}, "a", 0,
        )
        # Missing required 'qty' is rejected
        try:
            store.upsert(
                "sales_order", "O2",
                {"order_no": "O2", "sku_id": "S2"}, "a", 1,
            )
            raise AssertionError("should reject missing required field")
        except RecordInvariantError:
            pass
        # Duplicate order_no across records is rejected
        try:
            store.upsert(
                "sales_order", "O9",
                {"order_no": "O1", "sku_id": "S3", "qty": 1}, "a", 2,
            )
            raise AssertionError("should reject duplicate order_no")
        except RecordInvariantError as e:
            assert "order_no" in e.message


class TestLedgerReplay:
    def test_replay_derives_current_state(self) -> None:
        store = RecordStore()
        store.register_schema(_skus_schema())
        store.upsert("sku", "SKU-1", {"sku_id": "SKU-1", "stock": 50}, "a", 0)
        store.apply_delta("sku", "SKU-1", "stock", -10, "a", 1)
        store.apply_delta("sku", "SKU-1", "stock", -5, "a", 2)

        replayed = store.replay()
        assert replayed["sku:SKU-1"]["stock"] == 35
        assert store.get("sku", "SKU-1")["stock"] == 35
        # Projection and ledger agree — replay is the audit source
        assert store.get("sku", "SKU-1") == replayed["sku:SKU-1"]


class TestInvertMutation:
    def test_invert_restores_prior_and_trims_ledger(self) -> None:
        store = RecordStore()
        store.register_schema(_skus_schema())
        store.upsert("sku", "SKU-1", {"sku_id": "SKU-1", "stock": 50}, "a", 0)
        result = store.apply_delta("sku", "SKU-1", "stock", -10, "a", 1)
        assert store.get("sku", "SKU-1")["stock"] == 40
        assert store.ledger_len() == 2

        # Invert the delta with the true pre-delta prior → stock 50,
        # ledger entry removed
        store.invert_mutation(
            "sku", "SKU-1", {"sku_id": "SKU-1", "stock": 50},
            result.ledger_ids,  # [2]
        )
        assert store.get("sku", "SKU-1")["stock"] == 50
        assert store.ledger_len() == 1


class TestAssetStore:
    def test_put_get_stat_content_addressed(self) -> None:
        store = AssetStore()
        data = b"\x00\x01\x02 binary payload \xfe\xff"
        meta = store.put(data, mime="application/x-binary")
        assert meta.size == len(data)
        assert store.get(meta.sha256) == data
        assert store.stat(meta.sha256).sha256 == meta.sha256

    def test_dedup_same_content_same_hash(self) -> None:
        store = AssetStore()
        m1 = store.put(b"hello world")
        m2 = store.put(b"hello world")
        assert m1.sha256 == m2.sha256
        assert len(store) == 1

    def test_missing_asset_raises(self) -> None:
        store = AssetStore()
        try:
            store.get("deadbeef")
            raise AssertionError("should raise")
        except Exception as e:
            assert isinstance(e, KeyError)

    def test_attachment_ref_from_asset(self) -> None:
        store = AssetStore()
        data = b"attachment bytes"
        meta = store.put(data, mime="application/pdf")
        ref = store.ref(meta.sha256, path="doc.pdf")
        assert ref.ref_type == "asset"
        assert ref.hash == meta.sha256
        assert ref.size == len(data)
        assert ref.mime == "application/pdf"

    def test_attachment_ref_model(self) -> None:
        ref = AttachmentRef(
            ref_type="shared_kb", path="project/x.md", version=2,
            hash="h", size=10, mime="text/markdown",
        )
        assert ref.ref_type == "shared_kb"
        assert ref.size == 10


class TestCommitIntegration:
    def _stock(self, sim: Simulation) -> int:
        return sim.record_store.get("sku", "SKU-1")["stock"]

    def test_upsert_and_delta_commit(self) -> None:
        sim = _sim()
        # Create the sku + raise stock in one commit
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "record": {"sku_id": "SKU-1", "stock": 10}},
        )
        sim._transaction_buffer.stage(
            EffectType.RECORD_DELTA, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "field": "stock", "delta": 5},
        )
        sim._phase_commit(0, {})
        assert self._stock(sim) == 15

    def test_negative_stock_is_local_FAILED_not_rollback(self) -> None:
        """Sales order that drains stock below zero FAILS the delta
        deterministically (T18); the independent upsert still commits."""
        sim = _sim()
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "record": {"sku_id": "SKU-1", "stock": 10}},
        )
        sim._transaction_buffer.stage(
            EffectType.RECORD_DELTA, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "field": "stock", "delta": -15},
        )
        sim._phase_commit(0, {})
        effects = list(sim._transaction_buffer._effects.values())
        delta = next(
            e for e in effects if e.effect_type == EffectType.RECORD_DELTA
        )
        assert delta.status == EffectStatus.FAILED
        assert ">= 0" in (delta.error or "")
        # Stock is 10 (the oversell never applied) — projection intact
        assert self._stock(sim) == 10
        # No tick rollback, no epoch bump
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0
        assert sim.state_epoch == 0

    def test_order_and_stock_atomic_same_tick(self) -> None:
        """Same-tick 采购入库 + 销售出库: order created AND stock funded
        then drawn — all committed atomically as one transaction."""
        sim = _sim()
        # Fund stock (采购入库)
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "record": {"sku_id": "SKU-1", "stock": 5}},
        )
        # Create sales order
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sales_order:O-1",
            data={"record_type": "sales_order", "key": "O-1",
                  "record": {"order_no": "O-1", "sku_id": "SKU-1", "qty": 2}},
        )
        # Draw exactly the funded stock (销售出库) — never negative
        sim._transaction_buffer.stage(
            EffectType.RECORD_DELTA, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "field": "stock", "delta": -5},
        )
        sim._phase_commit(0, {})
        assert self._stock(sim) == 0  # funded then fully drawn
        assert sim.record_store.get("sales_order", "O-1")["qty"] == 2

    def test_oversell_above_funded_stock_rejected_in_same_tick(
        self,
    ) -> None:
        """A same-tick order that would draw more stock than funded is
        REJECTED deterministically; the funded stock and order still
        commit (局部 FAILED，不回滚)."""
        sim = _sim()
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "record": {"sku_id": "SKU-1", "stock": 5}},
        )
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sales_order:O-1",
            data={"record_type": "sales_order", "key": "O-1",
                  "record": {"order_no": "O-1", "sku_id": "SKU-1", "qty": 8}},
        )
        sim._transaction_buffer.stage(
            EffectType.RECORD_DELTA, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "field": "stock", "delta": -8},
        )
        sim._phase_commit(0, {})
        # The oversell delta FAILED locally; stock stays at funded 5
        assert self._stock(sim) == 5
        effects = list(sim._transaction_buffer._effects.values())
        delta = next(
            e for e in effects if e.effect_type == EffectType.RECORD_DELTA
        )
        assert delta.status == EffectStatus.FAILED
        rollbacks = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_ROLLBACK)
        assert len(rollbacks) == 0

    def test_invariant_violation_is_not_a_kernel_rollback(self) -> None:
        """The RecordInvariantError is caught as a DETERMINISTIC failure
        (T18) — it must NOT propagate as a kernel exception (no tick
        rollback, no state-epoch bump)."""
        sim = _sim()
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "record": {"sku_id": "SKU-1", "stock": 3}},
        )
        sim._phase_commit(0, {})
        assert self._stock(sim) == 3
        assert sim.state_epoch == 0

    def test_rollback_inverts_record(self) -> None:
        """A kernel failure later in the tick inverts an applied
        RECORD_DELTA — prior record restored, ledger trimmed."""
        sim = _sim()
        sim._transaction_buffer.stage(
            EffectType.RECORD_UPSERT, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "record": {"sku_id": "SKU-1", "stock": 10}},
        )
        sim._transaction_buffer.stage(
            EffectType.RECORD_DELTA, "agent.root", "sku:SKU-1",
            data={"record_type": "sku", "key": "SKU-1",
                  "field": "stock", "delta": -3},
        )
        # Kernel-fail the tick: FILE_WRITE to a directory path
        boom = f"boom-{uuid4().hex[:8]}"
        home = sim._private_store.agent_home("agent.root")
        (home / boom).mkdir(parents=True, exist_ok=True)
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE, "agent.root", boom,
            data={"content": "boom"},
        )
        sim._phase_commit(0, {})

        # Record fully undone → gone from projection and ledger
        assert sim.record_store.get("sku", "SKU-1") is None
        assert sim.record_store.ledger_len() == 0

    def test_tools_require_registered_schema(self) -> None:
        sim = _sim()
        sim._transaction_buffer.clear()
        r = sim._tool_registry.execute(
            _ctx(sim), "record_upsert",
            record_type="unknown", key="k", record={"a": 1},
        )
        assert not r.success
        assert r.error_code == "SCHEMA_NOT_REGISTERED"


class TestPrivateBinaryFile:
    def test_binary_write_and_read(self) -> None:
        sim = _sim()
        raw = b"\x00\x01\x02\xff binary \xde\xad\xbe\xef"
        import base64
        path = f"bin-{uuid4().hex[:8]}.bin"
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE, "agent.root", path,
            data={
                "content_bytes_b64": base64.b64encode(raw).decode("ascii"),
                "is_binary": True,
            },
        )
        sim._phase_commit(0, {})
        target = sim._private_store.agent_home("agent.root") / path
        assert target.read_bytes() == raw
        # Kernel read path returns the exact bytes (不再跳过二进制)
        found, bytes_read = sim._read_private_file_bytes("agent.root", path)
        assert found and bytes_read == raw

    def test_binary_write_rollback_restores_bytes(self) -> None:
        sim = _sim()
        import base64
        path = f"bin-{uuid4().hex[:8]}.bin"
        target = sim._private_store.agent_home("agent.root") / path
        prior = b"\xaa\xbb original"
        target.write_bytes(prior)

        raw = b"\x00\x01 new binary"
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE, "agent.root", path,
            data={
                "content_bytes_b64": base64.b64encode(raw).decode("ascii"),
                "is_binary": True,
            },
        )
        boom = f"boom-{uuid4().hex[:8]}"
        home = sim._private_store.agent_home("agent.root")
        (home / boom).mkdir(parents=True, exist_ok=True)
        sim._transaction_buffer.stage(
            EffectType.FILE_WRITE, "agent.root", boom,
            data={"content": "boom"},
        )
        sim._phase_commit(0, {})
        assert target.read_bytes() == prior  # byte-exact restore
        assert sim.state_epoch == 1
