"""RecordStore: typed structured records with invariants and a
replayable append-only ledger (SPEC §7.3, T10).

Model:
- Record types are registered from scenario-package schemas; registration
  validates the schema (field specs + invariant rules).
- ALL changes flow through effects (RECORD_UPSERT / RECORD_DELTA) and are
  applied by the commit pipeline; the store itself raises
  RecordInvariantError on violation → the commit path treats it as a
  DETERMINISTIC failure (local FAILED, T18 分级) — never a tick rollback.
- An append-only ledger holds every mutation; current state is a
  projection of the ledger and can be re-derived by replay (audit /
  对账 / 恢复 all derive from the ledger).
- Rollback / group failure: the effect's invert removes exactly the
  ledger entries it appended and restores the prior record (per-effect
  undo — the ledger stays an exact replay source).

N1c-1: RecordStore 归位为 Device 子类（SPEC §5.3，N1c 设备适配层）。
注册受控 uuid（范围级 DATA + 工具面 TOOL）+ InjectionDecl。
构造签名保持完全兼容（simulation.py 不变）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Literal

from my_team.devices.base import Device, EntityKind, InjectionDecl
from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from my_team.agent_runtime import ToolContext
    from my_team.transaction import TransactionBuffer


class RecordInvariantError(Exception):
    """Raised when a mutation would violate a registered invariant.

    Deterministic business failure under T18 — the commit path marks the
    effect FAILED locally; it NEVER triggers a full-tick rollback.
    """

    def __init__(self, record_type: str, key: str, message: str) -> None:
        self.record_type = record_type
        self.key = key
        self.message = message
        super().__init__(f"{record_type}:'{key}' invariant violation: {message}")


class FieldSpec(BaseModel):
    """Declaration of one record field."""

    name: str
    type: Literal["string", "int", "float", "date", "bool"]
    required: bool = True


class InvariantRule(BaseModel):
    """Declarative numeric record-level invariant (checked on mutation)."""

    kind: Literal["non_negative", "positive"]
    field: str = Field(default="", description="Numeric field the rule applies to")

    def check(
        self,
        record_type: str,
        key: str,
        data: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> None:
        """Raise RecordInvariantError when the rule is violated."""
        value = data.get(self.field, 0)
        numeric = isinstance(value, (int, float))
        if self.kind == "non_negative":
            ok = numeric and value >= 0
            expectation = ">= 0"
        else:  # positive
            ok = numeric and value > 0
            expectation = "> 0"
        if not ok:
            raise RecordInvariantError(
                record_type,
                key,
                f"{self.field} must be {expectation} (got {value!r})",
            )


class RecordSchema(BaseModel):
    """Schema for one record type. Registration validates it."""

    record_type: str
    fields: list[FieldSpec] = Field(default_factory=list)
    invariants: list[InvariantRule] = Field(default_factory=list)
    unique_fields: list[str] = Field(
        default_factory=list,
        description="Fields that must be globally unique across records "
        "of this type (e.g. order_no, sku_id)",
    )

    @model_validator(mode="after")
    def _validate(self) -> "RecordSchema":
        if not self.record_type.strip():
            raise ValueError("record_type must be non-empty")
        names = {f.name for f in self.fields}
        for uf in self.unique_fields:
            if uf not in names:
                raise ValueError(f"unique field '{uf}' not declared in fields")
        return self

    def validate_record(self, key: str, data: dict[str, Any]) -> None:
        """Field-level validation (type + required) — raises
        RecordInvariantError on malformed input."""
        for spec in self.fields:
            if spec.name not in data:
                if spec.required:
                    raise RecordInvariantError(
                        self.record_type,
                        key,
                        f"missing required field '{spec.name}'",
                    )
                continue
            value = data[spec.name]
            ok = {
                "string": isinstance(value, str),
                "int": isinstance(value, int) and not isinstance(value, bool),
                "float": isinstance(value, (int, float)),
                "date": isinstance(value, str),  # ISO date string; refined by scenarios
                "bool": isinstance(value, bool),
            }[spec.type]
            if not ok:
                raise RecordInvariantError(
                    self.record_type,
                    key,
                    f"field '{spec.name}' must be {spec.type} (got {value!r})",
                )


class LedgerEntry(BaseModel):
    """One append-only mutation record (replay source)."""

    ledger_id: int
    tick: int
    agent_id: str
    record_type: str
    key: str
    op: Literal["upsert", "delta"]
    before: dict[str, Any] | None
    after: dict[str, Any]
    delta_field: str | None = None
    delta_value: float | None = None


class MutationResult(BaseModel):
    """Result of a store mutation."""

    record_type: str
    key: str
    record: dict[str, Any]
    ledger_ids: list[int]
    version: int


class RecordStore(Device):
    """Typed record store with invariants + append-only ledger.

    N1c-1 设备归位：继承 Device，构造时注册受控 uuid
    （范围级 DATA + 工具面 TOOL）并声明 InjectionDecl。
    构造签名保持原样（simulation.py 兼容）。
    """

    def __init__(
        self,
        device_id: str | None = None,
        transaction_buffer: TransactionBuffer | None = None,
    ) -> None:
        Device.__init__(self, device_id)
        self._schemas: dict[str, RecordSchema] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._ledger: list[LedgerEntry] = []
        self._ledger_counter = 0
        self._version_counter: dict[str, int] = {}
        # N1c-2: injected kernel services for tool handlers
        self._transaction_buffer = transaction_buffer
        # N1c-1：注册设备受控实体
        # 范围级 DATA 实体 — 记录存储整体范围，InjectionDecl 引导 agent
        self.records_scope_id = self.register_entity(
            EntityKind.DATA,
            "record-store-scope",
            injection=InjectionDecl(
                content=(
                    "[RECORD_INSTRUCTION] 记录存储（RecordStore）管理结构化业务记录。\n"
                    "通过 record_upsert 工具新增/更新记录，"
                    "通过 record_delta 工具修改记录的数值字段（如库存）。\n"
                    "所有变更经效果层（STAGED_MUTATION）提交，"
                    "不变式违反为确定性业务失败（不触发 tick 回滚）。"
                ),
                source_tag="[RECORD_INSTRUCTION]",
            ),
        )
        # 工具面 TOOL 实体 — 采用 uuid5 派生值（adopt 机制）
        from my_team.tool_manifest import builtin_manifests

        _manifests = builtin_manifests()
        self.record_upsert_capability = self.register_entity(
            EntityKind.TOOL,
            "record_upsert",
            entity_id=_manifests["record_upsert"].capability,
        )
        self.record_delta_capability = self.register_entity(
            EntityKind.TOOL,
            "record_delta",
            entity_id=_manifests["record_delta"].capability,
        )

    # -- schema registration -------------------------------------------------

    def register_schema(self, schema: RecordSchema) -> None:
        """Register a record schema; duplicate type names are rejected."""
        if schema.record_type in self._schemas:
            raise ValueError(f"record type '{schema.record_type}' already registered")
        self._schemas[schema.record_type] = schema

    def has_schema(self, record_type: str) -> bool:
        return record_type in self._schemas

    def schema(self, record_type: str) -> RecordSchema:
        try:
            return self._schemas[record_type]
        except KeyError:
            raise RecordInvariantError(
                record_type,
                "",
                "record type not registered",
            ) from None

    # -- projections ----------------------------------------------------------

    def current_state(self) -> dict[str, dict[str, Any]]:
        """Current projected state (type:key → record)."""
        return dict(self._records)

    def get(self, record_type: str, key: str) -> dict[str, Any] | None:
        return self._records.get(f"{record_type}:{key}")

    def ledger_len(self) -> int:
        return len(self._ledger)

    def replay(self) -> dict[str, dict[str, Any]]:
        """Re-derive current state from the ledger (审计/对账/恢复)."""
        state: dict[str, dict[str, Any]] = {}
        for entry in self._ledger:
            full_key = f"{entry.record_type}:{entry.key}"
            if entry.op == "upsert":
                state[full_key] = dict(entry.after)
            else:  # delta
                current = dict(state.get(full_key, {}))
                current[entry.delta_field or ""] = current.get(entry.delta_field or "", 0) + (
                    entry.delta_value or 0
                )
                state[full_key] = current
        return state

    # -- mutations (effect apply side — invariant-checked) -------------------

    def upsert(
        self,
        record_type: str,
        key: str,
        data: dict[str, Any],
        agent_id: str,
        tick: int,
    ) -> MutationResult:
        """Upsert a record. Raises RecordInvariantError on violation
        (deterministic business failure — local FAILED, no tick rollback)."""
        schema = self.schema(record_type)
        schema.validate_record(key, data)

        full_key = f"{record_type}:{key}"
        existing = self._records.get(full_key)
        # Unique-field enforcement across records of this type
        for uf in schema.unique_fields:
            value = data.get(uf)
            if value is None:
                continue
            for other_key, other in self._records.items():
                if not other_key.startswith(f"{record_type}:"):
                    continue
                if other_key == full_key:
                    continue
                if other.get(uf) == value:
                    raise RecordInvariantError(
                        record_type,
                        key,
                        f"{uf}={value!r} already used by '{other_key}'",
                    )
        for rule in schema.invariants:
            rule.check(record_type, key, data, existing)

        before = dict(existing) if existing is not None else None
        record = dict(data)
        self._records[full_key] = record
        self._ledger_counter += 1
        entry = LedgerEntry(
            ledger_id=self._ledger_counter,
            tick=tick,
            agent_id=agent_id,
            record_type=record_type,
            key=key,
            op="upsert",
            before=before,
            after=record,
        )
        self._ledger.append(entry)
        self._version_counter[full_key] = self._version_counter.get(full_key, 0) + 1
        return MutationResult(
            record_type=record_type,
            key=key,
            record=record,
            ledger_ids=[entry.ledger_id],
            version=self._version_counter[full_key],
        )

    def apply_delta(
        self,
        record_type: str,
        key: str,
        field: str,
        delta: float,
        agent_id: str,
        tick: int,
    ) -> MutationResult:
        """Apply a numeric delta to a record field (e.g. stock movement).
        Invariants are checked on the RESULT — a negative stock is
        rejected deterministically."""
        schema = self.schema(record_type)
        full_key = f"{record_type}:{key}"
        existing = self._records.get(full_key)
        if existing is None:
            existing = {
                spec.name: (
                    ""
                    if spec.type == "string"
                    else 0
                    if spec.type in {"int", "float"}
                    else False
                    if spec.type == "bool"
                    else ""
                )
                for spec in schema.fields
            }
        current_value = existing.get(field, 0)
        if not isinstance(current_value, (int, float)):
            raise RecordInvariantError(
                record_type,
                key,
                f"cannot delta non-numeric field '{field}'",
            )
        before = dict(existing)
        record = dict(existing)
        record[field] = current_value + delta
        # Invariant check on the RESULT (库存非负等)
        for rule in schema.invariants:
            rule.check(record_type, key, record, existing)
        self._records[full_key] = record
        self._ledger_counter += 1
        entry = LedgerEntry(
            ledger_id=self._ledger_counter,
            tick=tick,
            agent_id=agent_id,
            record_type=record_type,
            key=key,
            op="delta",
            before=before,
            after=record,
            delta_field=field,
            delta_value=delta,
        )
        self._ledger.append(entry)
        self._version_counter[full_key] = self._version_counter.get(full_key, 0) + 1
        return MutationResult(
            record_type=record_type,
            key=key,
            record=record,
            ledger_ids=[entry.ledger_id],
            version=self._version_counter[full_key],
        )

    # -- per-effect invert (T18: 回滚=逆操作) --------------------------------

    def invert_mutation(
        self,
        record_type: str,
        key: str,
        prior_record: dict[str, Any] | None,
        ledger_ids: list[int],
    ) -> None:
        """Undo one mutation: remove its ledger entries and restore the
        prior record (or delete when prior is None)."""
        ledger_ids_set = set(ledger_ids)
        self._ledger[:] = [e for e in self._ledger if e.ledger_id not in ledger_ids_set]
        full_key = f"{record_type}:{key}"
        if prior_record is None:
            self._records.pop(full_key, None)
            self._version_counter.pop(full_key, None)
        else:
            self._records[full_key] = dict(prior_record)

    # -----------------------------------------------------------------------
    # N1c-2: Tool handler factories (record_upsert / record_delta)
    # -----------------------------------------------------------------------

    def make_handle_record_upsert(self) -> Callable[..., Any]:
        """Return the ``record_upsert`` tool handler bound to this device."""
        from my_team.agent_runtime import ToolResult

        transaction_buffer = self._transaction_buffer
        store = self

        def handle_record_upsert(
            context: ToolContext,
            record_type: str = "",
            key: str = "",
            record: dict[str, Any] | None = None,
            **_kw: Any,
        ) -> Any:
            if not record_type or not key:
                return ToolResult(
                    success=False,
                    error="record_upsert requires 'record_type' and 'key'",
                    error_code="INVALID_ARGUMENT",
                    retryable=False,
                    agent_id=context.agent_id,
                    tool_name="record_upsert",
                    tick=context.tick,
                )
            if not store.has_schema(record_type):
                return ToolResult(
                    success=False,
                    error=f"record type '{record_type}' not registered",
                    error_code="SCHEMA_NOT_REGISTERED",
                    retryable=False,
                    agent_id=context.agent_id,
                    tool_name="record_upsert",
                    tick=context.tick,
                )
            if transaction_buffer is not None:
                from my_team.transaction import EffectType

                transaction_buffer.stage(
                    effect_type=EffectType.RECORD_UPSERT,
                    agent_id=context.agent_id,
                    resource=f"{record_type}:{key}",
                    data={
                        "record_type": record_type,
                        "key": key,
                        "record": record or {},
                    },
                )
            return ToolResult(
                success=True,
                data={"staged": True},
                agent_id=context.agent_id,
                tool_name="record_upsert",
                tick=context.tick,
            )

        return handle_record_upsert

    def make_handle_record_delta(self) -> Callable[..., Any]:
        """Return the ``record_delta`` tool handler bound to this device."""
        from my_team.agent_runtime import ToolResult

        transaction_buffer = self._transaction_buffer
        store = self

        def handle_record_delta(
            context: ToolContext,
            record_type: str = "",
            key: str = "",
            field: str = "",
            delta: float = 0.0,
            **_kw: Any,
        ) -> Any:
            if not record_type or not key or not field:
                return ToolResult(
                    success=False,
                    error="record_delta requires 'record_type', 'key' and 'field'",
                    error_code="INVALID_ARGUMENT",
                    retryable=False,
                    agent_id=context.agent_id,
                    tool_name="record_delta",
                    tick=context.tick,
                )
            if not store.has_schema(record_type):
                return ToolResult(
                    success=False,
                    error=f"record type '{record_type}' not registered",
                    error_code="SCHEMA_NOT_REGISTERED",
                    retryable=False,
                    agent_id=context.agent_id,
                    tool_name="record_delta",
                    tick=context.tick,
                )
            if transaction_buffer is not None:
                from my_team.transaction import EffectType

                transaction_buffer.stage(
                    effect_type=EffectType.RECORD_DELTA,
                    agent_id=context.agent_id,
                    resource=f"{record_type}:{key}",
                    data={
                        "record_type": record_type,
                        "key": key,
                        "field": field,
                        "delta": float(delta),
                    },
                )
            return ToolResult(
                success=True,
                data={"staged": True},
                agent_id=context.agent_id,
                tool_name="record_delta",
                tick=context.tick,
            )

        return handle_record_delta
