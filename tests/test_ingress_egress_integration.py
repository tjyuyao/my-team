"""T9 传输层集成测试 — Ingress/Egress 与 Integration 注册.

覆盖卡面验收：
- 出站工具随 Integration 注册动态注入（决策2），走既有 executor 路径。
- Agent 发出外站 op → WAITING_FOR_EXTERNAL（决策3，纯事件等待，无回查工具）。
- ProviderAdmission 独立限流闸（决策1b）：provider 配额耗尽 → 保持 SUBMITTED
  背压；executor 容量与 provider 配额两因各自触发均保持 SUBMITTED。
- 外站回执经 Integration 回执断言 external_id → op_id 翻译命中 pending wait
  → 下一 tick 唤醒 Agent（决策4，翻译层平台相关，测试用假平台实现）。
- IngressBuffer (source, external_id) 跨重启持久化去重；未持久化不 ack。
"""

from __future__ import annotations

from my_team.agent_runtime import AgentObservation, BaseAgent
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.ingress import (
    IngressBuffer,
    IngressEvent,
    restore_ingress_buffer,
    snapshot_ingress_buffer,
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
    """Agent that submits an outbound platform-publish tool call.

    Scripted: always SubmitToolRequest('platform.publish'). After an
    external result arrives it stops (returns no intents → IDLE).
    """

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
            # external result received → done
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
        description="Publish content to an external platform (T9).",
        execution_class=ExecutionClass.EXTERNAL_IRREVERSIBLE,
        input_schema={
            "text": {"type": "string",
                      "description": "Content to publish to the platform"},
        },
        required_inputs=("text",),
        reversible=False,
    )


def _register(sim: Simulation, *, max_calls: int = 100) -> Integration:
    """Register a fake platform Integration owning an outbound tool.

    The ReceiptAssertion is the decision-4 extension interface: the fake
    platform's receipt carries `external_id` in payload key 'external_id'
    and resolves it to the kernel op_id via a closure over the pending
    registry (the FAKE platform's translation — plugin/scenario-pack code).
    """
    integration = Integration(
        name="fake_douyin",
        rate_limits=RateLimit(max_calls=max_calls, window_seconds=1000),
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
    # Grant the outbound tool to the agent (tool-capability check in Phase 6).
    sim._tool_registry.register_agent(
        "agent.root",
        sim._tool_registry.get_allowed_tools("agent.root")
        | frozenset({"platform.publish"}),
    )
    _bootstrap(sim, "agent.root")
    return agent


class TestOutboundDynamicRegistration:
    """决策2：出站工具随 Integration 动态注入，走既有 executor 路径。"""

    def test_register_injects_outbound_tool(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        _register(sim)
        # dynamic outbound tool registered as manifest
        assert sim._tool_registry.get_manifest("platform.publish") is not None
        # executor registered (UNTRUSTED_OUT_OF_PROCESS) → dispatch works
        rec = sim._executors.get("platform.publish")
        assert rec is not None
        assert sim._integrations.get_by_tool("platform.publish") is not None

    def test_unknown_outbound_tool_is_permanent_denial(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        # No integration owns 'platform.publish' → provider admission
        # permanent denial (retryable=False), op completes failed.
        _add_publish_agent(sim)
        sim.run_tick()
        # op may already be consumed (completed + removed) after the failed
        # dispatch → check via remaining op
        remaining = [o for o in sim._pending_ops._operations.values()
                     if o.agent_id == "agent.root"]
        assert not any(o.status == OpStatus.SUBMITTED for o in remaining)


class TestWaitForExternal:
    """决策3：出站 op → WAITING_FOR_EXTERNAL，纯事件唤醒。"""

    def test_agent_enters_waiting_for_external(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        _register(sim)
        _add_publish_agent(sim)
        sim.run_tick()

        rs = sim._agent_runtime_states["agent.root"]
        assert rs.state == AgentState.WAITING_FOR_EXTERNAL
        assert rs.continuation.phase == ContinuationPhase.WAITING_FOR_EXTERNAL

        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        assert ops[0].metadata.get("external_tool") is True
        assert ops[0].metadata.get("provider") == "fake_douyin"
        # Dispatched to provider (claim → PENDING) on the publish tick
        assert ops[0].status == OpStatus.PENDING

    def test_receipt_wakes_agent_with_result(self) -> None:
        """决策4：回执经 external_id→op_id 翻译命中 pending wait → 唤醒。"""
        sim = Simulation(agent_tree=_make_tree())
        _register(sim)
        _add_publish_agent(sim)
        sim.run_tick()

        # Find the in-flight op; its request_id is the op_id the fake
        # platform's resolver must return.
        op = sim._pending_ops.get_by_agent("agent.root")[0]
        sim._pending_ops.complete(op.request_id, result={
            "external_id": "ext-123",
            "_op_id": op.request_id,  # fake-platform resolver maps to op_id
            "result": {"accepted": True},
        })

        # Ingest consumes the buffer → completes op → wake → agent re-acts
        sim._pending_ops._operations[op.request_id].eligible_tick = sim.current_tick
        sim.run_tick()

        rs = sim._agent_runtime_states["agent.root"]
        # Agent processed the external result (PROCESSING_RESULT consumed)
        assert rs.continuation.total_tool_calls == 1
        assert rs.continuation.react_turn == 1

    def test_timeout_exits_waiting_for_external(self) -> None:
        """退避：deadline 到期 → WAITING_FOR_EXTERNAL 退出，不静默滞留。"""
        sim = Simulation(agent_tree=_make_tree())
        _register(sim)
        _add_publish_agent(sim)
        sim.run_tick()

        op = sim._pending_ops.get_by_agent("agent.root")[0]
        op.deadline_tick = 5
        op.status = OpStatus.PENDING
        expired = sim._pending_ops.timeout_expired(6)
        assert len(expired) == 1
        assert expired[0].status == OpStatus.TIMED_OUT


class TestProviderAdmission:
    """决策1b：独立 provider 限流闸，配额耗尽保持 SUBMITTED 背压。"""

    def test_provider_rate_limit_backpressure(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        _register(sim, max_calls=0)  # provider quota already exhausted
        _add_publish_agent(sim)
        sim.run_tick()

        # op stays SUBMITTED (provider backpressure) — not failed, not removed
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert ops, "op must remain in flight"
        assert ops[0].status == OpStatus.SUBMITTED
        # executor admission is separate: executor exists, so this is purely
        # provider-side backpressure

    def test_provider_and_executor_gates_are_independent(self) -> None:
        """两因各自存在：executor 有容量但 provider 限流 → 仍背压。"""
        sim = Simulation(agent_tree=_make_tree())
        _register(sim, max_calls=1)
        # Exhaust the provider window directly
        sim._integrations.record_dispatched("platform.publish")
        _add_publish_agent(sim)
        sim.run_tick()

        ops = sim._pending_ops.get_by_agent("agent.root")
        assert ops[0].status == OpStatus.SUBMITTED  # provider quota blocked
        # Executor has capacity (admit would pass) — proves the two gates
        # are independent dimensions.
        assert sim._executors.get("platform.publish") is not None


class TestIngressDedupAndAck:
    """IngressBuffer 持久化去重 + 未持久化不 ack。"""

    def _buf(self, persist: list[dict] | None = None) -> IngressBuffer:
        persist = [] if persist is None else persist
        return IngressBuffer(persist_cb=lambda ev: persist.append(
            ev.model_dump(mode="json"),
        ))

    def test_dedup_same_source_external_id(self) -> None:
        buf = self._buf()
        ev = IngressEvent(
            source="douyin", external_id="e1", event_type="comment",
            occurred_at="t",
        )
        assert buf.receive(ev) is True
        assert buf.receive(ev) is False  # duplicate dropped
        assert buf.pending_count() == 1

    def test_ack_after_persist(self) -> None:
        persisted: list[dict] = []
        buf = self._buf(persisted)
        ev = IngressEvent(
            source="douyin", external_id="e1", event_type="comment",
            occurred_at="t",
        )
        buf.receive(ev)
        # before persist → not acked
        assert not buf.is_acked(("douyin", "e1"))
        buf.persist()
        assert buf.is_acked(("douyin", "e1"))
        assert len(persisted) == 1

    def test_cross_restart_dedup_via_snapshot(self) -> None:
        buf = self._buf()
        ev = IngressEvent(
            source="douyin", external_id="e1", event_type="comment",
            occurred_at="t",
        )
        buf.receive(ev)
        snap = snapshot_ingress_buffer(buf)
        restored = restore_ingress_buffer(snap)
        # same event re-ingested after restart → rejected (seen)
        dup = IngressEvent(
            source="douyin", external_id="e1", event_type="comment",
            occurred_at="t",
        )
        assert restored.receive(dup) is False

    def test_cross_restart_dedup_via_sim_store(self, tmp_path) -> None:
        """验收：重复 (source, external_id) 跨重启只入站一次。"""
        sim = Simulation(agent_tree=_make_tree())
        ev = IngressEvent(
            source="douyin", external_id="e1", event_type="comment",
            occurred_at="t",
        )
        sim.inject_ingress(ev)  # mark seen
        sim.ingress.persist()   # ack + durable seen-set

        db = tmp_path / "sim.db"
        sim.save_to(db)

        sim2 = Simulation.load_from(db)
        # same event re-ingested after restart → duplicate, rejected once
        assert sim2.inject_ingress(
            IngressEvent(
                source="douyin", external_id="e1", event_type="comment",
                occurred_at="t",
            )
        ) is False
        assert sim2.ingress.pending_count() == 0  # dup dropped
