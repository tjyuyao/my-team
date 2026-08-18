"""T8a: KB read-side tools (kb_read / kb_list / kb_search).

Verifies the read gap closure (OI-004 §1.5 — previously write-only):
- Agents can read KB entries they hold permission for; unauthorized
  reads are refused (success=False, permission_denied).
- kb_list only surfaces permitted paths.
- kb_search matches path/content case-insensitively, respects base_path
  and limit, and NEVER surfaces unauthorized entries (deny-by-default —
  no "exists but not allowed" leak).
- All three are READ_ONLY, registered via the builtin path; read audit
  (shared_kb.read) is recorded without content.
"""
from __future__ import annotations

from my_team.agent_runtime import (
    ActionPlan,
    AgentAction,
    BaseAgent,
    ToolContext,
    action_plan_to_intents,
)
from my_team.agent_tree import AgentTree
from my_team.audit import AuditEventType
from my_team.shared_kb import PermissionRule
from my_team.simulation import Simulation
from my_team.tool_manifest import ExecutionClass, builtin_manifests


def _ctx(sim: Simulation, agent_id: str) -> ToolContext:
    return ToolContext(
        agent_id=agent_id, tick=0,
        allowed_tools=sim._tool_registry.get_allowed_tools(agent_id),
    )


def _kb_sim() -> Simulation:
    """Two agents; root may read/list/search the whole project KB,
    research only 'project/research/*'."""
    tree = AgentTree.from_dict({
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research"],
                "tools": ["read", "write", "kb_read", "kb_list", "kb_search"],
                "can_delegate": True,
                "metadata": {"bootstrap": True},
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research",
                "role": "worker",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "kb_read", "kb_list", "kb_search"],
                "can_delegate": False,
                "metadata": {"bootstrap": False},
            },
        ],
    })
    sim = Simulation(agent_tree=tree)
    sim._permission_engine.add_rules([
        PermissionRule(
            scope="project/*", principal="agent.root",
            allow=["read", "list", "create", "write", "kb_write"],
        ),
        # kb_list checks LIST on the requested prefix itself; a
        # wildcard scope ("project/*") covers paths UNDER it but not the
        # directory root — exact-scope rules grant the root listings.
        PermissionRule(
            scope="project", principal="agent.root",
            allow=["read", "list"],
        ),
        PermissionRule(
            scope="project/research/*", principal="agent.research",
            allow=["read", "list", "create", "write", "kb_write"],
        ),
        PermissionRule(
            scope="project/research", principal="agent.research",
            allow=["list"],
        ),
    ])
    # Seed a few entries
    sim._shared_kb.create(
        path="project/research/notes.md", agent_id="agent.root",
        content="agent design review notes", tick=0,
    )
    sim._shared_kb.create(
        path="project/roadmap.md", agent_id="agent.root",
        content="Q3 roadmap for the team", tick=0,
    )
    sim._shared_kb.create(
        path="project/research/secret.md", agent_id="agent.root",
        content="hidden internal detail", tick=0,
    )
    return sim


class TestKbReadTool:
    def test_read_within_permission(self) -> None:
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_read",
            path="project/research/notes.md",
        )
        assert result.success
        assert result.data["content"] == "agent design review notes"
        assert result.data["version"] == 1
        assert result.data["last_modified_by"] == "agent.root"

    def test_read_unauthorized_refused(self) -> None:
        """research cannot read a path outside its scope."""
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.research"), "kb_read",
            path="project/roadmap.md",
        )
        assert not result.success
        assert result.error_code == "permission_denied"

    def test_read_missing_not_found(self) -> None:
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_read",
            path="project/nope.md",
        )
        assert not result.success
        assert result.error_code == "not_found"

    def test_read_records_audit_without_content(self) -> None:
        sim = _kb_sim()
        sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_read",
            path="project/roadmap.md",
        )
        entries = sim.audit_log.for_event_type(AuditEventType.SHARED_KB_READ)
        assert len(entries) == 1
        assert entries[0].details["path"] == "project/roadmap.md"
        assert "content" not in (entries[0].details or {})


class TestKbListTool:
    def test_list_scoped_by_permission(self) -> None:
        """research lists its own subtree; an unauthorized PREFIX is
        refused (越权前缀被拒)."""
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.research"), "kb_list",
            base_path="project/research",
        )
        assert result.success
        assert set(result.data["paths"]) == {
            "project/research/notes.md",
            "project/research/secret.md",
        }

    def test_list_unauthorized_prefix_refused(self) -> None:
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.research"), "kb_list",
            base_path="project/",
        )
        assert not result.success
        assert result.error_code == "permission_denied"

    def test_list_root_sees_all(self) -> None:
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_list",
            base_path="project/",
        )
        assert set(result.data["paths"]) == {
            "project/research/notes.md",
            "project/roadmap.md",
            "project/research/secret.md",
        }


class TestKbSearchTool:
    def test_search_matches_path_and_content_case_insensitive(self) -> None:
        sim = _kb_sim()
        # content hit (case-insensitive)
        r1 = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search", query="REVIEW",
        )
        assert {h["path"] for h in r1.data["results"]} == {
            "project/research/notes.md",
        }
        # path hit
        r2 = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search", query="roadmap",
        )
        assert {h["path"] for h in r2.data["results"]} == {
            "project/roadmap.md",
        }

    def test_search_never_leaks_unauthorized(self) -> None:
        """research searches 'team' — roadmap.md (an unauthorized hit)
        must NOT appear; a permitted hit ('notes') does."""
        sim = _kb_sim()
        # An unauthorized path matches the query but is filtered out
        leak = sim._tool_registry.execute(
            _ctx(sim, "agent.research"), "kb_search", query="team",
        )
        assert leak.success
        assert leak.data["results"] == []  # roadmap.md invisible
        # Positive control: an authorized content hit is returned
        ok = sim._tool_registry.execute(
            _ctx(sim, "agent.research"), "kb_search", query="notes",
        )
        assert [h["path"] for h in ok.data["results"]] == [
            "project/research/notes.md",
        ]

    def test_search_snippet_not_full_content(self) -> None:
        sim = _kb_sim()
        result = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search", query="notes",
        )
        hit = result.data["results"][0]
        assert hit["snippet"] == "agent design review notes"  # ≤200 chars
        assert "content" not in hit

    def test_search_honors_limit_and_base_path(self) -> None:
        sim = _kb_sim()
        sim._shared_kb.create(
            path="project/a.md", agent_id="agent.root", content="same", tick=0,
        )
        sim._shared_kb.create(
            path="project/b.md", agent_id="agent.root", content="same", tick=0,
        )
        r = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search",
            query="same", limit=1,
        )
        assert len(r.data["results"]) == 1
        r = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search",
            query="same", base_path="project/research",
        )
        # a.md/b.md live under project/ (outside the prefix); notes.md
        # and secret.md contain no 'same' → zero hits within the scope
        assert r.data["results"] == []
        r = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search",
            query="review", base_path="project/research",
        )
        assert {h["path"] for h in r.data["results"]} == {
            "project/research/notes.md",
        }

    def test_search_rejects_empty_query(self) -> None:
        sim = _kb_sim()
        r = sim._tool_registry.execute(
            _ctx(sim, "agent.root"), "kb_search", query="  ",
        )
        assert not r.success
        assert r.error_code == "INVALID_ARGUMENT"


class TestManifestContract:
    def test_kb_tools_are_read_only_and_registered(self) -> None:
        manifests = builtin_manifests()
        for name in ("kb_read", "kb_list", "kb_search"):
            m = manifests[name]
            assert m.execution_class is ExecutionClass.READ_ONLY
            assert m.deterministic and m.idempotent
            assert m.filesystem_scopes == ("shared-kb",)
        assert manifests["kb_read"].capabilities == ("kb:read",)
        assert manifests["kb_list"].capabilities == ("kb:list",)
        assert manifests["kb_search"].capabilities == ("kb:search",)
        assert manifests["kb_search"].required_inputs == ("query",)

    def test_kb_read_in_real_tick(self) -> None:
        """A real tick: the agent reads the KB through the tool pipeline."""
        sim = _kb_sim()

        class KBReaderAgent(BaseAgent):
            def decide_intents(self, observation, continuation=None):
                plan = ActionPlan(
                    agent_id="agent.root",
                    tick=observation.tick,
                    actions=[AgentAction(
                        action_type="kb_read",
                        tool_name="kb_read",
                        payload={"path": "project/roadmap.md"},
                    )],
                )
                return action_plan_to_intents(plan)

        agent = KBReaderAgent("agent.root")
        agent._tool_registry = sim._tool_registry
        sim._runtimes["agent.root"] = agent
        sim.run_tick()

        reads = sim.audit_log.for_event_type(AuditEventType.SHARED_KB_READ)
        assert len(reads) >= 1
        assert reads[0].details["path"] == "project/roadmap.md"
