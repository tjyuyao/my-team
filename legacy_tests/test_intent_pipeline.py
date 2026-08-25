"""Tests for the v0.6.0 Intent-based pipeline.

Verifies:
- decide_intents() conversion from legacy ActionPlan
- SubmitLLMRequest → PendingOperation → WAITING_FOR_LLM
- Full async LLM cycle: submit → response → re-activation → processing
- LLMAgent async semantics (never blocks)
- Intent staging in _phase_act (SendEmail/Delegate/WritePrivateFile)
"""

from __future__ import annotations

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    AgentObservation,
    BaseAgent,
    action_plan_to_intents,
)
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.intent import (
    DelegateIntent,
    SendEmailIntent,
    SubmitLLMRequest,
    SubmitToolRequest,
    WritePrivateFileIntent,
)
from my_team.pending_ops import OpStatus, OpType
from my_team.simulation import Simulation


def _make_tree() -> AgentTree:
    return AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })


class IntentAgent(BaseAgent):
    """Agent that produces Intents directly."""

    def __init__(
        self,
        agent_id: str,
        intents_fn: object | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._intents_fn = intents_fn

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ):
        if self._intents_fn:
            return self._intents_fn(observation, continuation)
        return []


class TestActionPlanConversion:
    """Legacy ActionPlan → Intent conversion."""

    def test_delegate_conversion(self) -> None:
        plan = ActionPlan(agent_id="agent.root", tick=0, actions=[
            AgentAction(
                action_type="delegate",
                tool_name="delegate",
                payload={
                    "recipient_agent_id": "agent.research",
                    "task_title": "Task",
                    "task_description": "Desc",
                },
            ),
        ])
        intents = action_plan_to_intents(plan)
        assert len(intents) == 1
        assert isinstance(intents[0], DelegateIntent)
        assert intents[0].recipient_agent_id == "agent.research"
        assert intents[0].task_title == "Task"

    def test_send_email_conversion(self) -> None:
        plan = ActionPlan(agent_id="agent.root", tick=0, actions=[
            AgentAction(
                action_type="send_email",
                tool_name="send_email",
                payload={"to": ["agent.research"], "subject": "Hi", "body": "Hello"},
            ),
        ])
        intents = action_plan_to_intents(plan)
        assert len(intents) == 1
        assert isinstance(intents[0], SendEmailIntent)
        assert intents[0].to == ["agent.research"]

    def test_write_conversion(self) -> None:
        plan = ActionPlan(agent_id="agent.root", tick=0, actions=[
            AgentAction(
                action_type="write",
                tool_name="write",
                payload={"path": "f.txt", "content": "data"},
            ),
        ])
        intents = action_plan_to_intents(plan)
        assert len(intents) == 1
        assert isinstance(intents[0], WritePrivateFileIntent)
        assert intents[0].path == "f.txt"

    def test_read_conversion(self) -> None:
        plan = ActionPlan(agent_id="agent.root", tick=0, actions=[
            AgentAction(
                action_type="read",
                tool_name="read",
                payload={"path": "f.txt"},
            ),
        ])
        intents = action_plan_to_intents(plan)
        assert len(intents) == 1
        assert isinstance(intents[0], SubmitToolRequest)
        assert intents[0].tool_name == "read"


class TestSubmitLLMRequestFlow:
    """SubmitLLMRequest → PendingOperation → WAITING_FOR_LLM."""

    def test_submit_llm_creates_pending_op(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        agent = IntentAgent(
            "agent.root",
            intents_fn=lambda obs, cont: [SubmitLLMRequest(agent_id="agent.root")],
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()

        # Agent should be WAITING_FOR_LLM
        state = sim._agent_runtime_states["agent.root"]
        assert state.state == AgentState.WAITING_FOR_LLM
        assert state.continuation.phase == ContinuationPhase.WAITING_FOR_LLM
        assert state.continuation.total_llm_calls == 1

        # Pending op registered
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1
        assert ops[0].op_type == OpType.LLM_REQUEST

    def test_llm_agent_never_blocks(self) -> None:
        """LLMAgent.decide_intents returns SubmitLLMRequest, not a blocking call."""
        from my_team.llm_agent import LLMAgent
        from my_team.llm_gateway import LLMGateway
        from my_team.models.llm import LLMProviderConfig

        gateway = LLMGateway()
        gateway.register_profile("default", LLMProviderConfig(
            provider="mock",
            model="mock-model",
        ))

        sim = Simulation(agent_tree=_make_tree())
        agent = LLMAgent(
            agent_id="agent.root",
            llm_gateway=gateway,
            llm_profile="default",
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Run tick — should NOT block on LLM call
        sim.run_tick()

        state = sim._agent_runtime_states["agent.root"]
        assert state.state == AgentState.WAITING_FOR_LLM
        ops = sim._pending_ops.get_by_agent("agent.root")
        assert len(ops) == 1

    def test_full_async_cycle(self) -> None:
        """submit → response → re-activation → process result → write file."""
        sim = Simulation(agent_tree=_make_tree())

        def intents_fn(obs, continuation):
            if continuation and continuation.last_llm_result:
                content = continuation.last_llm_result.get("content", "")
                return [
                    WritePrivateFileIntent(
                        agent_id="agent.root",
                        path="result.txt",
                        content=content,
                    ),
                ]
            return [SubmitLLMRequest(agent_id="agent.root", messages=())]

        agent = IntentAgent("agent.root", intents_fn=intents_fn)
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Tick 0: submit LLM request
        sim.run_tick()
        assert sim._agent_runtime_states["agent.root"].state == AgentState.WAITING_FOR_LLM

        # Simulate response arriving
        op = sim._pending_ops.get_by_agent("agent.root")[0]
        op.status = OpStatus.PENDING
        sim._pending_ops.complete(op.request_id, result={"content": "async response"})

        # Tick 1: ingest delivers → agent re-activated → writes file
        sim.run_tick()

        home = sim._private_store.agent_home("agent.root")
        result_file = home / "result.txt"
        assert result_file.exists()
        assert result_file.read_text() == "async response"
        assert sim._agent_runtime_states["agent.root"].state == AgentState.IDLE


class TestIntentStaging:
    """Intent → StagedEffect conversion in _phase_act."""

    def test_send_email_intent_stages_email(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        agent = IntentAgent(
            "agent.research",
            intents_fn=lambda obs, cont: [
                SendEmailIntent(
                    agent_id="agent.research",
                    to=["agent.root"],
                    subject="Status",
                    body="Done",
                ),
            ],
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = agent

        # Enqueue a BOOTSTRAP event for research and allow it in wake condition
        from my_team.models.activation import WakeCondition, WakeEventType, WakeupEvent
        cond = sim.scheduler.get_wake_condition("agent.research")
        sim.scheduler.update_wake_condition(
            "agent.research",
            WakeCondition(
                event_types=cond.event_types | {WakeEventType.BOOTSTRAP},
                wake_at_tick=0,
            ),
        )
        sim.scheduler.enqueue_event(WakeupEvent(
            event_type=WakeEventType.BOOTSTRAP,
            target_agent_id="agent.research",
            tick=0, visible_at_tick=0,
            source_agent_id="system",
        ))

        sim.run_tick()

        # Email should be staged and committed
        assert len(sim._mail_system._all_emails) == 1
        email = list(sim._mail_system._all_emails.values())[0]
        assert email.subject == "Status"
        assert email.to == ["agent.root"]

    def test_delegate_intent_stages_task_and_email(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        agent = IntentAgent(
            "agent.root",
            intents_fn=lambda obs, cont: [
                DelegateIntent(
                    agent_id="agent.root",
                    recipient_agent_id="agent.research",
                    task_title="Intent Task",
                    task_description="via intent",
                ),
            ],
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()

        assert len(sim.task_tree.get_active_tasks()) == 1
        assert len(sim._mail_system._all_emails) == 1

    def test_write_file_intent_stages_write(self) -> None:
        sim = Simulation(agent_tree=_make_tree())
        agent = IntentAgent(
            "agent.root",
            intents_fn=lambda obs, cont: [
                WritePrivateFileIntent(
                    agent_id="agent.root",
                    path="note.txt",
                    content="hello",
                ),
            ],
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()

        home = sim._private_store.agent_home("agent.root")
        note = home / "note.txt"
        assert note.exists()
        assert note.read_text() == "hello"

    def test_wait_for_event_intent(self) -> None:
        """WaitForEventIntent transitions agent to waiting state."""
        from my_team.models.intent import WaitForEventIntent

        sim = Simulation(agent_tree=_make_tree())
        agent = IntentAgent(
            "agent.root",
            intents_fn=lambda obs, cont: [
                WaitForEventIntent(
                    agent_id="agent.root",
                    waiting_state="waiting_for_mail",
                    event_types=["new_email"],
                ),
            ],
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        sim.run_tick()

        state = sim._agent_runtime_states["agent.root"]
        assert state.state == AgentState.WAITING_FOR_MAIL


class TestLLMAgentContinuation:
    """LLMAgent uses continuation for result processing."""

    def test_llm_agent_processes_pending_result(self) -> None:
        from my_team.llm_agent import LLMAgent
        from my_team.llm_gateway import LLMGateway
        from my_team.models.llm import LLMProviderConfig

        gateway = LLMGateway()
        gateway.register_profile("default", LLMProviderConfig(
            provider="mock",
            model="mock-model",
        ))

        sim = Simulation(agent_tree=_make_tree())
        agent = LLMAgent(
            agent_id="agent.root",
            llm_gateway=gateway,
            llm_profile="default",
        )
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent

        # Set up continuation with a pending result
        rs = sim._agent_runtime_states["agent.root"]
        rs.continuation.receive_llm_result(
            {"content": "", "tool_calls": []}, tick=1,
        )

        # Decide should parse the result (empty → no intents), NOT submit new request
        from my_team.agent_runtime import AgentObservation
        obs = AgentObservation(
            agent_id="agent.root",
            tick=1,
            emails=[],
            task_states={},
            shared_kb_snapshot={},
            lock_states={},
            private_workspace_path="/tmp",
        )
        intents = agent.decide_intents(
            observation=obs,
            continuation=rs.continuation,
        )
        assert len(intents) == 0  # empty result → no actions
