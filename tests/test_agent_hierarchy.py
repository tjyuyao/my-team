"""Tests for Agent organization tree: loading, validation, traversal.

Per KANBAN task: 2026-08-17-agent-hierarchy
"""

import json
import tempfile
from pathlib import Path

import pytest

from my_team.agent_tree import (
    AgentNotFoundError,
    AgentTree,
    AgentTreeError,
    ChildNotDeclaredError,
    CycleDetectedError,
    DuplicateAgentIdError,
    MultipleRootsError,
    NoRootError,
    ParentChildMismatchError,
)
from my_team.models.agent import AgentConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_agent(
    agent_id: str,
    parent_id: str | None = None,
    children: list[str] | None = None,
    tools: list[str] | None = None,
    can_delegate: bool = False,
) -> AgentConfig:
    return AgentConfig(
        agent_id=agent_id,
        display_name=agent_id.replace(".", " ").replace("_", " ").title(),
        role="custom",
        parent_id=parent_id,
        children=children or [],
        tools=tools or ["read", "write", "ls"],
        can_delegate=can_delegate,
    )


@pytest.fixture
def sample_tree_data() -> dict:
    """A valid 6-agent tree matching SPEC §17 example."""
    return {
        "agents": [
            {
                "agent_id": "agent.root",
                "display_name": "Root Agent",
                "role": "root_decision_agent",
                "parent_id": None,
                "children": ["agent.research", "agent.planning", "agent.review"],
                "tools": ["read", "write", "ls", "delegate"],
                "can_delegate": True,
            },
            {
                "agent_id": "agent.research",
                "display_name": "Research Agent",
                "role": "research_manager",
                "parent_id": "agent.root",
                "children": ["agent.web_research", "agent.data_analysis"],
                "tools": ["read", "write", "ls", "send_email", "delegate"],
                "can_delegate": True,
            },
            {
                "agent_id": "agent.planning",
                "display_name": "Planning Agent",
                "role": "planning_manager",
                "parent_id": "agent.root",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
            },
            {
                "agent_id": "agent.review",
                "display_name": "Review Agent",
                "role": "review_manager",
                "parent_id": "agent.root",
                "children": ["agent.quality_check"],
                "tools": ["read", "write", "ls", "send_email", "delegate"],
                "can_delegate": True,
            },
            {
                "agent_id": "agent.web_research",
                "display_name": "Web Research Agent",
                "role": "web_researcher",
                "parent_id": "agent.research",
                "children": [],
                "tools": ["read", "write", "ls", "send_email", "web_search"],
                "can_delegate": False,
            },
            {
                "agent_id": "agent.data_analysis",
                "display_name": "Data Analysis Agent",
                "role": "data_analyst",
                "parent_id": "agent.research",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
            },
            {
                "agent_id": "agent.quality_check",
                "display_name": "Quality Check Agent",
                "role": "quality_check_agent",
                "parent_id": "agent.review",
                "children": [],
                "tools": ["read", "write", "ls", "send_email"],
                "can_delegate": False,
            },
        ],
    }


@pytest.fixture
def sample_tree(sample_tree_data) -> AgentTree:
    return AgentTree.from_dict(sample_tree_data)


# ---------------------------------------------------------------------------
# Loading & basic structure
# ---------------------------------------------------------------------------

class TestAgentTreeLoading:
    """Tests for loading agent trees from config."""

    def test_load_from_dict(self, sample_tree):
        assert len(sample_tree) == 7
        assert sample_tree.root_id == "agent.root"

    def test_load_from_json_file(self, sample_tree_data, tmp_path):
        config_file = tmp_path / "agents.json"
        config_file.write_text(json.dumps(sample_tree_data))
        tree = AgentTree.from_config_file(config_file)
        assert len(tree) == 7

    def test_missing_config_file(self):
        with pytest.raises(FileNotFoundError):
            AgentTree.from_config_file("/nonexistent/agents.json")

    def test_empty_agents_list(self):
        with pytest.raises(NoRootError):
            AgentTree.from_dict({"agents": []})

    def test_single_agent_no_parent(self):
        tree = AgentTree.from_dict({
            "agents": [{
                "agent_id": "agent.root",
                "display_name": "Root",
                "role": "root",
                "parent_id": None,
                "children": [],
                "tools": ["read"],
                "can_delegate": False,
            }],
        })
        assert tree.root_id == "agent.root"
        assert len(tree) == 1


# ---------------------------------------------------------------------------
# Validation invariants (SPEC §18)
# ---------------------------------------------------------------------------

class TestValidation:
    """Tests for tree invariant checks."""

    def test_duplicate_agent_id_rejected(self):
        agents = [
            _make_agent("agent.dup", children=["agent.dup.child"]),
            _make_agent("agent.dup", parent_id="agent.dup"),
        ]
        with pytest.raises(DuplicateAgentIdError) as exc_info:
            AgentTree(agents)
        assert exc_info.value.agent_id == "agent.dup"

    def test_multiple_roots_rejected(self):
        agents = [
            _make_agent("agent.root1"),
            _make_agent("agent.root2"),
        ]
        with pytest.raises(MultipleRootsError) as exc_info:
            AgentTree(agents)
        assert len(exc_info.value.roots) == 2

    def test_no_root_rejected(self):
        agents = [
            _make_agent("agent.a", parent_id="agent.b"),
            _make_agent("agent.b", parent_id="agent.a"),
        ]
        with pytest.raises((NoRootError, CycleDetectedError)):
            AgentTree(agents)

    def test_cycle_detected(self):
        agents = [
            _make_agent("agent.root", children=["agent.a"]),
            _make_agent("agent.a", parent_id="agent.root", children=["agent.b"]),
            _make_agent("agent.b", parent_id="agent.a", children=["agent.a"]),
        ]
        with pytest.raises(CycleDetectedError):
            AgentTree(agents)

    def test_parent_child_mismatch_rejected(self):
        """parent lists child, but child's parent_id points elsewhere."""
        agents = [
            _make_agent("agent.root", children=["agent.a"]),
            _make_agent("agent.a", parent_id="agent.root"),
            _make_agent("agent.b", parent_id="agent.root"),
        ]
        # agent.b declares parent=agent.root but is NOT in root's children
        # This should be caught
        with pytest.raises(ParentChildMismatchError):
            AgentTree(agents)

    def test_child_not_declared_rejected(self):
        """Parent lists a child_id that doesn't exist as an agent."""
        agents = [
            _make_agent("agent.root", children=["agent.nonexistent"]),
        ]
        with pytest.raises(ChildNotDeclaredError):
            AgentTree(agents)

    def test_valid_tree_no_errors(self, sample_tree):
        """The sample tree should pass all validation."""
        assert sample_tree.root_id == "agent.root"
        assert len(sample_tree) == 7


# ---------------------------------------------------------------------------
# Traversal
# ---------------------------------------------------------------------------

class TestTraversal:
    """Tests for tree traversal utilities."""

    def test_get_agent(self, sample_tree):
        agent = sample_tree.get("agent.research")
        assert agent.agent_id == "agent.research"
        assert agent.display_name == "Research Agent"

    def test_get_nonexistent_agent(self, sample_tree):
        with pytest.raises(AgentNotFoundError):
            sample_tree.get("agent.nonexistent")

    def test_contains(self, sample_tree):
        assert "agent.root" in sample_tree
        assert "agent.nonexistent" not in sample_tree

    def test_children(self, sample_tree):
        children = sample_tree.children("agent.root")
        child_ids = {c.agent_id for c in children}
        assert child_ids == {"agent.research", "agent.planning", "agent.review"}

    def test_children_of_leaf(self, sample_tree):
        assert sample_tree.children("agent.web_research") == []

    def test_child_ids(self, sample_tree):
        ids = sample_tree.child_ids("agent.research")
        assert ids == ["agent.web_research", "agent.data_analysis"]

    def test_parent(self, sample_tree):
        parent = sample_tree.parent("agent.research")
        assert parent.agent_id == "agent.root"

    def test_parent_of_root(self, sample_tree):
        assert sample_tree.parent("agent.root") is None

    def test_siblings(self, sample_tree):
        siblings = sample_tree.siblings("agent.research")
        sibling_ids = {s.agent_id for s in siblings}
        assert sibling_ids == {"agent.planning", "agent.review"}

    def test_siblings_of_root(self, sample_tree):
        assert sample_tree.siblings("agent.root") == []

    def test_is_ancestor(self, sample_tree):
        assert sample_tree.is_ancestor("agent.root", "agent.web_research")
        assert sample_tree.is_ancestor("agent.research", "agent.web_research")
        assert not sample_tree.is_ancestor("agent.web_research", "agent.root")
        assert not sample_tree.is_ancestor("agent.research", "agent.planning")

    def test_ancestors(self, sample_tree):
        ancestors = sample_tree.ancestors("agent.web_research")
        ancestor_ids = [a.agent_id for a in ancestors]
        assert ancestor_ids == ["agent.research", "agent.root"]

    def test_depth(self, sample_tree):
        assert sample_tree.depth("agent.root") == 0
        assert sample_tree.depth("agent.research") == 1
        assert sample_tree.depth("agent.web_research") == 2

    def test_subtree_ids(self, sample_tree):
        ids = sample_tree.subtree_ids("agent.research")
        assert set(ids) == {"agent.research", "agent.web_research", "agent.data_analysis"}

    def test_can_delegate_to(self, sample_tree):
        assert sample_tree.can_delegate_to("agent.root", "agent.research")
        assert sample_tree.can_delegate_to("agent.root", "agent.planning")
        assert not sample_tree.can_delegate_to("agent.research", "agent.planning")
        assert not sample_tree.can_delegate_to("agent.web_research", "agent.research")

    def test_all_ids(self, sample_tree):
        assert set(sample_tree.all_ids) == {
            "agent.root", "agent.research", "agent.planning",
            "agent.review", "agent.web_research", "agent.data_analysis",
            "agent.quality_check",
        }


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    """Tests for tree serialization."""

    def test_to_dict(self, sample_tree):
        data = sample_tree.to_dict()
        assert "agents" in data
        assert "root_id" in data
        assert data["root_id"] == "agent.root"
        assert len(data["agents"]) == 7

    def test_roundtrip(self, sample_tree):
        data = sample_tree.to_dict()
        tree2 = AgentTree.from_dict(data)
        assert len(tree2) == len(sample_tree)
        assert tree2.root_id == sample_tree.root_id
        for agent_id in sample_tree.all_ids:
            assert tree2.get(agent_id).role == sample_tree.get(agent_id).role
