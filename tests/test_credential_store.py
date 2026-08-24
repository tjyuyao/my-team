"""T12b: CredentialStore — 引用式凭证存储（SPEC §7.5 / §12.4）.

覆盖卡面验收：
- ``credential_ref`` 可解析为可用凭证；无引用 / 引用不存在时明确报错。
- 密钥明文不出现在 Journal / 审计 / DB（persistence 组件快照 + SQLite
  文件字节）/ prompt（ContextCompiler 输出）——所有可观测面枚举断言。
- 出站工具/Integration 通过 ``credential_ref`` 取用凭证，不持明文：
  内核 dispatch 只在 ref 不可解析时永久拒绝（value-free has() 门禁），
  解析发生在 executor/plugin 边界。
- 测试全部使用假凭证（env 注入 / 测试专用加密文件），无真实密钥。

Date: 2026-08-18
"""

from __future__ import annotations

import json

import pytest

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.credential_store import (
    CredentialDecryptError,
    CredentialNotFoundError,
    CredentialStore,
    CredentialStoreError,
    EncryptedFileCredentialBackend,
    EnvCredentialBackend,
    MissingCredentialRefError,
)
from my_team.integration import (
    Integration,
    RateLimit,
    ReceiptAssertion,
)
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import Intent, SubmitToolRequest
from my_team.pending_ops import OpStatus
from my_team.simulation import Simulation
from my_team.tool_manifest import ExecutionClass, ToolManifest

# Fake test credential — never a real secret.
FAKE_SECRET = "sk-fake-test-secret-0123"
FAKE_SECRET_2 = "sk-fake-test-secret-4567"
ENV_VAR = "MY_TEAM_FAKE_PLATFORM_TOKEN"


# ---------------------------------------------------------------------------
# CredentialStore 单元测试
# ---------------------------------------------------------------------------


class TestResolveErrors:
    """无引用 / 引用不存在 → 明确报错。"""

    def test_resolve_empty_ref_raises(self) -> None:
        store = CredentialStore()
        with pytest.raises(MissingCredentialRefError):
            store.resolve("")
        with pytest.raises(MissingCredentialRefError):
            store.resolve("   ")

    def test_resolve_unknown_backend_kind_raises(self) -> None:
        store = CredentialStore(backends=[EnvCredentialBackend()])
        with pytest.raises(CredentialNotFoundError):
            store.resolve("kms:some-key")

    def test_resolve_missing_env_var_raises(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_VAR, raising=False)
        store = CredentialStore(backends=[EnvCredentialBackend()])
        with pytest.raises(CredentialNotFoundError):
            store.resolve(f"env:{ENV_VAR}")

    def test_resolve_no_backend_and_no_default_raises(self) -> None:
        store = CredentialStore()
        with pytest.raises(CredentialNotFoundError):
            store.resolve("plain-name")

    def test_has_empty_ref_is_false(self) -> None:
        assert CredentialStore().has("") is False
        assert CredentialStore().has("   ") is False


class TestEnvBackend:
    """env 后端：解析 = 读环境变量，值不进 store 状态。"""

    def test_resolve_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_VAR, FAKE_SECRET)
        store = CredentialStore(backends=[EnvCredentialBackend()])
        assert store.resolve(f"env:{ENV_VAR}") == FAKE_SECRET
        assert store.has(f"env:{ENV_VAR}") is True

    def test_has_missing_env_var_is_false(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_VAR, raising=False)
        store = CredentialStore(backends=[EnvCredentialBackend()])
        assert store.has(f"env:{ENV_VAR}") is False

    def test_default_backend_for_unprefixed_ref(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_VAR, FAKE_SECRET)
        store = CredentialStore(
            backends=[EnvCredentialBackend()],
            default_backend="env",
        )
        assert store.resolve(ENV_VAR) == FAKE_SECRET
        assert store.has(ENV_VAR) is True

    def test_duplicate_backend_kind_rejected(self) -> None:
        store = CredentialStore()
        store.register(EnvCredentialBackend())
        with pytest.raises(ValueError):
            store.register(EnvCredentialBackend())


class TestEncryptedFileBackend:
    """加密文件后端：落盘无明文；密钥 = 口令派生。"""

    def _store(self, tmp_path, passphrase: str = "correct-horse") -> CredentialStore:
        backend = EncryptedFileCredentialBackend(
            tmp_path / "creds.json", passphrase,
        )
        backend.put("douyin_token", FAKE_SECRET)
        backend.put("taobao_token", FAKE_SECRET_2)
        return CredentialStore(backends=[backend])

    def test_roundtrip_and_no_plaintext_on_disk(self, tmp_path) -> None:
        store = self._store(tmp_path)
        assert store.resolve("file:douyin_token") == FAKE_SECRET
        assert store.resolve("file:taobao_token") == FAKE_SECRET_2
        assert store.has("file:douyin_token") is True

        raw = (tmp_path / "creds.json").read_bytes()
        assert FAKE_SECRET.encode() not in raw
        assert FAKE_SECRET_2.encode() not in raw
        # File is a structured encrypted document, not a secret dump.
        doc = json.loads(raw.decode("utf-8"))
        assert doc["version"] == 1
        assert "ciphertext" in doc and "salt" in doc and "tag" in doc
        assert "douyin_token" not in doc  # entry names not even in the clear

    def test_wrong_passphrase_raises(self, tmp_path) -> None:
        self._store(tmp_path)
        wrong = EncryptedFileCredentialBackend(
            tmp_path / "creds.json", "wrong-passphrase",
        )
        with pytest.raises(CredentialDecryptError):
            wrong.resolve("douyin_token")
        with pytest.raises(CredentialDecryptError):
            wrong.contains("douyin_token")

    def test_missing_entry_raises(self, tmp_path) -> None:
        store = self._store(tmp_path)
        with pytest.raises(CredentialNotFoundError):
            store.resolve("file:not-there")

    def test_missing_file_is_empty_store(self, tmp_path) -> None:
        backend = EncryptedFileCredentialBackend(
            tmp_path / "creds.json", "pass",
        )
        assert backend.contains("x") is False
        with pytest.raises(CredentialNotFoundError):
            backend.resolve("x")

    def test_put_overwrites_and_preserves_others(self, tmp_path) -> None:
        store = self._store(tmp_path)
        assert store.resolve("file:douyin_token") == FAKE_SECRET
        backend = EncryptedFileCredentialBackend(
            tmp_path / "creds.json", "correct-horse",
        )
        backend.put("douyin_token", FAKE_SECRET_2)
        assert backend.resolve("douyin_token") == FAKE_SECRET_2
        assert backend.resolve("taobao_token") == FAKE_SECRET_2  # preserved

    def test_corrupt_file_raises(self, tmp_path) -> None:
        path = tmp_path / "creds.json"
        path.write_text("not json at all", encoding="utf-8")
        backend = EncryptedFileCredentialBackend(path, "pass")
        with pytest.raises(CredentialStoreError):
            backend.resolve("x")


class TestSnapshotIsMetadataOnly:
    """snapshot() 只暴露引用与条目名，永不暴露值。"""

    def test_snapshot_never_exposes_secrets(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv(ENV_VAR, FAKE_SECRET)
        file_backend = EncryptedFileCredentialBackend(
            tmp_path / "creds.json", "pass",
        )
        file_backend.put("douyin_token", FAKE_SECRET)
        store = CredentialStore(
            backends=[EnvCredentialBackend(), file_backend],
        )
        snap_text = json.dumps(store.snapshot(), sort_keys=True)
        assert FAKE_SECRET not in snap_text
        assert "douyin_token" in snap_text
        assert "env" in snap_text and "file" in snap_text


# ---------------------------------------------------------------------------
# 内核集成：出站工具经 credential_ref 取用凭证，密钥不出可观测面
# ---------------------------------------------------------------------------


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": [],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
        ],
    })


def _bootstrap(sim: Simulation, agent_id: str) -> None:
    from my_team.models.activation import WakeCondition, WakeEventType, WakeupEvent
    cond = sim.scheduler.get_wake_condition(agent_id)
    sim.scheduler.update_wake_condition(
        agent_id,
        WakeCondition(
            event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
            wake_at_tick=0,
        ),
    )
    sim.scheduler.enqueue_event(WakeupEvent(
        event_type=WakeEventType.BOOTSTRAP,
        target_agent_id=agent_id,
        tick=0, visible_at_tick=0,
        source_agent_id="system",
    ))


class PublishAgent(BaseAgent):
    """Scripted agent: submits one outbound 'platform.publish' call."""

    def __init__(self, agent_id: str, **kwargs: object) -> None:
        super().__init__(agent_id=agent_id, **kwargs)

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_tool_result
        ):
            return []
        return [
            SubmitToolRequest(
                agent_id=self._agent_id,
                tool_name="platform.publish",
                arguments={"text": "new chapter"},
                timeout_ticks=10,
            ),
        ]


def _platform_manifest() -> ToolManifest:
    return ToolManifest(
        name="platform.publish",
        version="1.0.0",
        description="Publish content to an external platform (T9/T12b).",
        execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
        input_schema={
            "text": {"type": "string",
                      "description": "Content to publish to the platform"},
        },
        required_inputs=("text",),
        reversible=False,
    )


def _register(sim: Simulation, *, credential_ref: str = "") -> Integration:
    integration = Integration(
        name="fake_douyin",
        credential_ref=credential_ref,
        rate_limits=RateLimit(max_calls=100, window_seconds=1000),
        manifests=[_platform_manifest()],
        ingress_event_types=["publish_ack"],
        receipt=ReceiptAssertion(
            external_id_field="external_id",
            op_id_resolver=lambda ext_id, payload: payload.get("_op_id"),
        ),
    )
    sim.register_integration(integration)
    return integration


def _add_publish_agent(sim: Simulation) -> PublishAgent:
    agent = PublishAgent("agent.root")
    agent._tool_registry = sim._tool_registry
    sim._runtimes["agent.root"] = agent
    sim._tool_registry.register_agent(
        "agent.root",
        sim._tool_registry.get_allowed_tools("agent.root")
        | frozenset({"platform.publish"}),
    )
    _bootstrap(sim, "agent.root")
    return agent


def _observable_text(sim: Simulation) -> str:
    """枚举全部可观测面：Journal / 审计 / DB 组件快照 / prompt 上下文。

    返回拼接文本；断言其中不含密钥明文即覆盖 SPEC §12.4 不变量 4。
    """
    parts: list[str] = []
    # Journal（含 journal 内嵌的 audit_events）
    parts.append(json.dumps(
        [r.model_dump(mode="json") for r in sim._journal.records],
        sort_keys=True,
    ))
    # 审计（独立视图）
    parts.append(json.dumps(
        [e.model_dump(mode="json") for e in sim._audit_log.entries],
        sort_keys=True,
    ))
    # DB：persistence 组件快照（save_to 的序列化源）
    parts.append(json.dumps(sim._collect_state(), sort_keys=True))
    # prompt：ContextCompiler 输出（每 agent 的观察）
    snapshot = sim._build_snapshot(sim.current_tick)
    for cfg in sim._agent_tree:
        parts.append(json.dumps(
            sim._context_compiler.compile(cfg, snapshot),
            sort_keys=True,
        ))
    return "\n".join(parts)


class TestOutboundCredentialResolution:
    """出站工具/Integration 经 credential_ref 取用凭证，不持明文。"""

    def test_resolve_and_complete_out_of_band(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Harness/plugin 侧经 resolve() 取凭证完成外站调用；全可观测面
        无密钥明文。"""
        monkeypatch.setenv(ENV_VAR, FAKE_SECRET)
        sim = Simulation(agent_tree=_make_tree())
        store = CredentialStore(backends=[EnvCredentialBackend()])
        sim.set_credential_store(store)
        integration = _register(sim, credential_ref=f"env:{ENV_VAR}")
        _add_publish_agent(sim)
        sim.run_tick()

        # 出站 op 已 dispatch（PENDING）；内核只持 ref，不持明文。
        rs = sim._agent_runtime_states["agent.root"]
        assert rs.state == AgentState.WAITING_FOR_EXTERNAL
        op = sim._pending_ops.get_by_agent("agent.root")[0]
        assert op.status == OpStatus.PENDING
        assert op.metadata.get("provider") == "fake_douyin"
        # kernel 可观测面此刻仍无明文
        assert FAKE_SECRET not in _observable_text(sim)

        # executor/plugin 边界：凭 ref 解析出可用凭证（不落任何可观测面）
        resolved = sim.credential_store.resolve(
            integration.credential_ref,
        )
        assert resolved == FAKE_SECRET  # 可用凭证

        # 假平台完成外站调用（结果不回显密钥）→ op 完成 → 唤醒
        sim._pending_ops.complete(op.request_id, result={
            "external_id": "ext-1",
            "_op_id": op.request_id,
            "result": {"accepted": True},
        })
        sim._pending_ops._operations[op.request_id].eligible_tick = (
            sim.current_tick
        )
        sim.run_tick()

        # DB 文件字节同样无明文
        db = tmp_path / "sim.db"
        sim.save_to(db)
        assert FAKE_SECRET.encode() not in db.read_bytes()

        assert FAKE_SECRET not in _observable_text(sim)
        # 控制器可经 has() 门禁确认 ref 可解析，不泄露值
        assert sim.credential_store.has(f"env:{ENV_VAR}") is True

    def test_unresolvable_ref_is_permanent_denial(self) -> None:
        """credential_ref 声明了但不可解析 → 永久拒绝（非背压），
        审计可追溯，且错误信息不含任何密钥值。"""
        sim = Simulation(agent_tree=_make_tree())
        # 默认 store 无后端 → has() = False → 门禁触发
        _register(sim, credential_ref=f"env:{ENV_VAR}")
        _add_publish_agent(sim)
        sim.run_tick()

        remaining = [o for o in sim._pending_ops._operations.values()
                     if o.agent_id == "agent.root"]
        assert not any(o.status == OpStatus.SUBMITTED for o in remaining)

        denials = [
            e for e in sim._audit_log.entries
            if e.details.get("status") == "credential_unresolvable"
        ]
        assert denials, "credential_unresolvable 审计必须存在"
        assert "is not resolvable" in (denials[-1].error or "")

        text = _observable_text(sim)
        assert FAKE_SECRET not in text  # 值从未出现（哪怕环境里根本没有它）
        assert f"env:{ENV_VAR}" in text  # ref 可审计，值不可见

    def test_empty_credential_ref_skips_gate(self) -> None:
        """无 credential_ref（空引用）的工具不受门禁影响（既有行为）。"""
        sim = Simulation(agent_tree=_make_tree())
        _register(sim)  # credential_ref 默认 ""
        _add_publish_agent(sim)
        sim.run_tick()

        op = sim._pending_ops.get_by_agent("agent.root")[0]
        assert op.status == OpStatus.PENDING  # 照常 dispatch
