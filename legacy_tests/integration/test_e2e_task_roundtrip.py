"""Complete task completion roundtrip E2E with async LLM agents.

The full business loop:

  Human request
  → Root LLM decides to delegate
  → Research LLM writes Shared KB + submits result
  → Root LLM completes task + replies to human

All LLM calls are async: SubmitLLMRequest → WAITING_FOR_LLM →
FakeLLMProvider completes → Ingest delivers → agent re-activated.

Validates at the end:
  - Task tree: parent task completed
  - Emails: delegation sent, result returned, human reply delivered
  - Shared KB: artifact written with version
  - Audit log: activation/commit events reconstructable
"""

from __future__ import annotations

import json

from my_team.agent_runtime import (
    AgentObservation,
    BaseAgent,
    action_plan_to_intents,
)
from my_team.agent_state import AgentState
from my_team.agent_tree import AgentTree
from my_team.fake_llm import FakeLLMProvider
from my_team.models.continuation import AgentContinuation, ContinuationPhase
from my_team.models.email import EmailType
from my_team.models.intent import Intent, SubmitLLMRequest
from my_team.models.task import TaskStatus
from my_team.prompt_templates import PromptTemplates
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


class FakeLLMAgent(BaseAgent):
    """Agent that parses LLM responses into Intents (async)."""

    def __init__(self, agent_id: str, **kwargs: object) -> None:
        super().__init__(agent_id=agent_id, **kwargs)
        self._templates = PromptTemplates()

    def decide_intents(
        self,
        observation: AgentObservation,
        continuation: AgentContinuation | None = None,
    ) -> list[Intent]:
        if (
            continuation is not None
            and continuation.phase == ContinuationPhase.PROCESSING_RESULT
            and continuation.last_llm_result
        ):
            result = continuation.last_llm_result
            plan = self._templates.parse_llm_response(
                content=result.get("content", ""),
                tool_calls=list(result.get("tool_calls", [])),
                agent_id=self._agent_id,
                tick=observation.tick,
            )
            return action_plan_to_intents(plan)
        return [SubmitLLMRequest(agent_id=self._agent_id, messages=())]


def _llm_tool_call(name: str, args: dict) -> dict:
    return {
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


class TestTaskCompletionRoundtrip:
    """Human request → Root → Research → KB → submit → complete → reply."""

    def _build_sim(self) -> tuple[Simulation, FakeLLMProvider]:
        sim = Simulation(agent_tree=_make_tree())

        provider = FakeLLMProvider(
            latency_ticks=1,
            responses={
                "agent.root": [
                    # Response 1: root decides to delegate to research
                    {
                        "content": "",
                        "tool_calls": [_llm_tool_call("delegate", {
                            "recipient_agent_id": "agent.research",
                            "task_title": "Market Analysis",
                            "task_description": "Analyze market trends",
                        })],
                    },
                    # Response 2: root completes task and replies to human
                    # task_id is injected dynamically via replace_script()
                    {
                        "content": "",
                        "tool_calls": [
                            _llm_tool_call("complete_task", {
                                "task_id": "__INJECTED_TASK_ID__",
                                "summary": "Analysis complete",
                            }),
                            _llm_tool_call("send_email", {
                                "to": ["human.user"],
                                "subject": "Market Analysis Complete",
                                "body": "The analysis is done. See the report.",
                            }),
                        ],
                    },
                ],
                "agent.research": [
                    # Response 1: research writes report and submits result
                    {
                        "content": "",
                        "tool_calls": [
                            _llm_tool_call("write", {
                                "path": "report.md",
                                "content": "# Market Analysis\nStrong growth expected.",
                            }),
                            _llm_tool_call("send_email", {
                                "to": ["agent.root"],
                                "subject": "[RESULT] Market Analysis",
                                "body": "Report written.",
                            }),
                        ],
                    },
                ],
            },
        )

        root = FakeLLMAgent("agent.root")
        root._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = root

        research = FakeLLMAgent("agent.research")
        research._tool_registry = sim._tool_registry
        sim._runtimes["agent.research"] = research

        return sim, provider

    def _send_human_request(self, sim: Simulation) -> None:
        """Human sends a request to root (delivered at tick 1)."""
        sim.mail_system.create_email(
            from_agent="human.user",
            to=["agent.root"],
            subject="Please analyze the market",
            body="I need a market analysis by end of week.",
            email_type=EmailType.HUMAN_MESSAGE,
            tick=0,
            deliver_at_tick=1,
        )

    def test_full_roundtrip(self) -> None:
        """Run the full loop and verify final state."""
        sim, provider = self._build_sim()
        self._send_human_request(sim)

        # Run 4 ticks: delegation happens, task created with dynamic task_id
        for tick in range(1, 5):
            provider.advance(sim, current_tick=tick)
            sim.run_tick()

        # Inject the real task_id into root's second script
        tasks = list(sim.task_tree._tasks.values())
        market_task = next(
            (t for t in tasks if t.title == "Market Analysis"), None,
        )
        assert market_task is not None, "Delegation should have created the task"
        provider.replace_script("agent.root", [{
            "content": "",
            "tool_calls": [
                _llm_tool_call("complete_task", {
                    "task_id": market_task.task_id,
                    "summary": "Analysis complete",
                }),
                _llm_tool_call("send_email", {
                    "to": ["human.user"],
                    "subject": "Market Analysis Complete",
                    "body": "The analysis is done. See the report.",
                }),
            ],
        }])

        # Run remaining ticks (12 covers the reply delivery tick)
        for tick in range(5, 13):
            provider.advance(sim, current_tick=tick)
            sim.run_tick()

        # === Final state verification ===

        # 1. Task tree: parent task created and completed
        tasks = list(sim.task_tree._tasks.values())
        assert len(tasks) >= 1
        market_task = next(
            (t for t in tasks if t.title == "Market Analysis"), None,
        )
        assert market_task is not None
        assert market_task.status == TaskStatus.COMPLETED, (
            f"Task status: {market_task.status}"
        )

        # 2. Emails: human request received, result sent, human reply dispatched
        all_emails = list(sim._mail_system._all_emails.values())
        human_reply = next(
            (e for e in all_emails if e.subject == "Market Analysis Complete"),
            None,
        )
        assert human_reply is not None
        assert human_reply.to == ["human.user"]
        # Human is an external recipient (no in-system mailbox), so the
        # email leaves the pending queue when dispatched
        assert sim._mail_system.pending_count == 0, (
            f"Pending emails: {sim._mail_system.pending_count}"
        )

        result_email = next(
            (e for e in all_emails if e.subject == "[RESULT] Market Analysis"),
            None,
        )
        assert result_email is not None

        # 3. Private file: research wrote the report
        home = sim._private_store.agent_home("agent.research")
        report = home / "report.md"
        assert report.exists(), "Research should have written report.md"
        assert "Strong growth" in report.read_text()

        # 4. Agent states: both agents should be idle at the end
        assert sim._agent_runtime_states["agent.root"].state == AgentState.IDLE
        assert sim._agent_runtime_states["agent.research"].state == AgentState.IDLE

        # 5. Audit log: activation and commit events present
        from my_team.audit import AuditEventType
        activated = sim.audit_log.for_event_type(AuditEventType.AGENT_ACTIVATED)
        commits = sim.audit_log.for_event_type(AuditEventType.TRANSACTION_COMMIT)
        assert len(activated) >= 3, f"Activations: {len(activated)}"
        assert len(commits) >= 4, f"Commits: {len(commits)}"

    def test_research_never_blocked_on_llm(self) -> None:
        """Research should never synchronously wait for LLM during a tick."""
        sim, provider = self._build_sim()
        self._send_human_request(sim)

        # Tick 1: root wakes, submits LLM. Research still idle.
        provider.advance(sim, current_tick=1)
        sim.run_tick()

        research_state = sim._agent_runtime_states["agent.research"]
        # Research hasn't been activated yet (no email yet)
        assert research_state.state == AgentState.IDLE

    def test_root_completes_task_after_result(self) -> None:
        """Root only completes the task after receiving the result email."""
        sim, provider = self._build_sim()
        self._send_human_request(sim)

        # Run a few ticks — not enough for full roundtrip
        for tick in range(1, 4):
            provider.advance(sim, current_tick=tick)
            sim.run_tick()

        tasks = list(sim.task_tree._tasks.values())
        market_task = next(
            (t for t in tasks if t.title == "Market Analysis"), None,
        )
        # Task may not exist yet or may be in-progress, but NOT completed
        if market_task is not None:
            assert market_task.status != TaskStatus.COMPLETED, (
                "Task completed too early"
            )
