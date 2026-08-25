"""Agent organization tree: loading, validation, and traversal.

Per SPEC §2.2, §4.1, §11.1, §18:
- Static tree, immutable during runtime
- Each agent has exactly one parent (except root)
- No cycles in organization relationships
- Delegation only to direct children
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from my_team.models.agent import AgentConfig


class AgentTreeError(Exception):
    """Base exception for agent tree validation errors."""


class DuplicateAgentIdError(AgentTreeError):
    """Raised when duplicate agent_id found in configuration."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Duplicate agent_id: '{agent_id}'")


class CycleDetectedError(AgentTreeError):
    """Raised when organization tree contains a cycle."""

    def __init__(self, cycle_path: list[str]) -> None:
        self.cycle_path = cycle_path
        cycle_str = " -> ".join(cycle_path)
        super().__init__(f"Cycle detected: {cycle_str}")


class MultipleRootsError(AgentTreeError):
    """Raised when tree has more than one root node."""

    def __init__(self, roots: list[str]) -> None:
        self.roots = roots
        super().__init__(f"Multiple root agents found: {roots}")


class NoRootError(AgentTreeError):
    """Raised when tree has no root node."""

    def __init__(self) -> None:
        super().__init__("No root agent found (agent with parent_id=None)")


class ParentChildMismatchError(AgentTreeError):
    """Raised when parent's children list doesn't match child's parent_id."""

    def __init__(self, agent_id: str, declared_parent: str, actual_parent: str) -> None:
        self.agent_id = agent_id
        self.declared_parent = declared_parent
        self.actual_parent = actual_parent
        super().__init__(
            f"Agent '{agent_id}' declares parent '{declared_parent}' "
            f"but is listed as child of '{actual_parent}'"
        )


class ChildNotDeclaredError(AgentTreeError):
    """Raised when a child is listed in parent's children but not in agents."""

    def __init__(self, parent_id: str, missing_child: str) -> None:
        self.parent_id = parent_id
        self.missing_child = missing_child
        super().__init__(
            f"Agent '{parent_id}' lists child '{missing_child}' "
            f"but no such agent exists in configuration"
        )


class AgentNotFoundError(AgentTreeError):
    """Raised when referencing a non-existent agent."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        super().__init__(f"Agent not found: '{agent_id}'")


class AgentTree:
    """Static organization tree of agents.

    Loaded once from configuration and immutable thereafter.
    Provides traversal utilities (find parent, children, siblings).
    """

    def __init__(self, agents: list[AgentConfig]) -> None:
        self._root_id: str | None = None
        self._children_map: dict[str, list[str]] = {}
        self._parent_map: dict[str, str | None] = {}

        # Check for duplicate IDs before building the dict
        seen_ids: set[str] = set()
        for agent in agents:
            if agent.agent_id in seen_ids:
                raise DuplicateAgentIdError(agent.agent_id)
            seen_ids.add(agent.agent_id)

        self._agents: dict[str, AgentConfig] = {}
        for agent in agents:
            self._agents[agent.agent_id] = agent
            self._parent_map[agent.agent_id] = agent.parent_id

        self._build_children_map()
        self._validate()

    # -- Construction & validation ------------------------------------------

    def _build_children_map(self) -> None:
        """Build parent -> children mapping from agent configs."""
        for agent in self._agents.values():
            if agent.agent_id not in self._children_map:
                self._children_map[agent.agent_id] = list(agent.children)

    def _validate(self) -> None:
        """Run all invariant checks. Raises AgentTreeError on failure."""
        self._check_duplicate_ids()
        self._check_root_count()
        self._check_parent_child_consistency()
        self._check_no_cycles()

    def _check_duplicate_ids(self) -> None:
        """Invariant: each agent_id must be unique (SPEC §18.1)."""
        seen: set[str] = set()
        for agent_id in self._agents:
            if agent_id in seen:
                raise DuplicateAgentIdError(agent_id)
            seen.add(agent_id)

    def _check_root_count(self) -> None:
        """Invariant: exactly one root agent (SPEC §18.1)."""
        roots = [
            aid for aid, agent in self._agents.items()
            if agent.parent_id is None
        ]
        if len(roots) == 0:
            raise NoRootError()
        if len(roots) > 1:
            raise MultipleRootsError(roots)
        self._root_id = roots[0]

    def _check_parent_child_consistency(self) -> None:
        """Invariant: parent's children list matches child's parent_id."""
        for agent in self._agents.values():
            if agent.parent_id is not None:
                if agent.parent_id not in self._agents:
                    raise AgentNotFoundError(agent.parent_id)
                parent = self._agents[agent.parent_id]
                if agent.agent_id not in parent.children:
                    raise ParentChildMismatchError(
                        agent.agent_id,
                        declared_parent=agent.parent_id,
                        actual_parent="(not listed in any parent's children)",
                    )

        # Also check that all declared children exist
        for agent in self._agents.values():
            for child_id in agent.children:
                if child_id not in self._agents:
                    raise ChildNotDeclaredError(agent.agent_id, child_id)

    def _check_no_cycles(self) -> None:
        """Invariant: organization relationships must not form cycles (SPEC §18.2).

        Uses parent-link traversal: each node must reach the root by following
        parent_id without revisiting a node. Also checks that children lists
        don't create cycles by doing DFS on the children graph.
        """
        # 1) Check parent-link chains: every node must reach root without loops
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {aid: WHITE for aid in self._agents}

        def dfs_parent(node: str, path: list[str]) -> None:
            """DFS following parent edges upward."""
            color[node] = GRAY
            path.append(node)

            par = self._parent_map.get(node)
            if par is not None and par in self._agents:
                if color[par] == GRAY:
                    cycle_start = path.index(par)
                    raise CycleDetectedError(path[cycle_start:] + [par])
                elif color[par] == WHITE:
                    dfs_parent(par, path)

            path.pop()
            color[node] = BLACK

        for agent_id in self._agents:
            if color[agent_id] == WHITE:
                dfs_parent(agent_id, [])

        # 2) Check children edges for cycles (e.g. A lists B as child, B lists A as child)
        children_color: dict[str, int] = {aid: WHITE for aid in self._agents}

        def dfs_children(node: str, path: list[str]) -> None:
            """DFS following children edges downward."""
            children_color[node] = GRAY
            path.append(node)

            for child_id in self._children_map.get(node, []):
                if child_id in self._agents:
                    if children_color[child_id] == GRAY:
                        cycle_start = path.index(child_id)
                        raise CycleDetectedError(path[cycle_start:] + [child_id])
                    elif children_color[child_id] == WHITE:
                        dfs_children(child_id, path)

            path.pop()
            children_color[node] = BLACK

        for agent_id in self._agents:
            if children_color[agent_id] == WHITE:
                dfs_children(agent_id, [])

    # -- Queries ------------------------------------------------------------

    @property
    def root_id(self) -> str:
        """ID of the root agent."""
        assert self._root_id is not None, "Root not initialized"
        return self._root_id

    @property
    def root(self) -> AgentConfig:
        """Root agent configuration."""
        assert self._root_id is not None, "No root agent set"
        return self._agents[self._root_id]

    def get(self, agent_id: str) -> AgentConfig:
        """Get agent config by ID. Raises AgentNotFoundError if missing."""
        if agent_id not in self._agents:
            raise AgentNotFoundError(agent_id)
        return self._agents[agent_id]

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._agents

    def __len__(self) -> int:
        return len(self._agents)

    def __iter__(self):
        return iter(self._agents.values())

    @property
    def all_ids(self) -> list[str]:
        """All agent IDs in the tree."""
        return list(self._agents.keys())

    def children(self, agent_id: str) -> list[AgentConfig]:
        """Direct children of an agent."""
        self.get(agent_id)  # validates existence
        child_ids = self._children_map.get(agent_id, [])
        return [self._agents[cid] for cid in child_ids]

    def child_ids(self, agent_id: str) -> list[str]:
        """Direct child IDs of an agent."""
        self.get(agent_id)
        return list(self._children_map.get(agent_id, []))

    def parent(self, agent_id: str) -> AgentConfig | None:
        """Parent agent, or None for root."""
        self.get(agent_id)
        par_id = self._parent_map.get(agent_id)
        if par_id is None:
            return None
        return self._agents[par_id]

    def siblings(self, agent_id: str) -> list[AgentConfig]:
        """Agents sharing the same parent (excludes self)."""
        agent = self.get(agent_id)
        if agent.parent_id is None:
            return []
        return [
            a for a in self.children(agent.parent_id)
            if a.agent_id != agent_id
        ]

    def is_ancestor(self, ancestor_id: str, descendant_id: str) -> bool:
        """Check if ancestor_id is an ancestor of descendant_id."""
        self.get(ancestor_id)
        self.get(descendant_id)
        current: str | None = descendant_id
        while current is not None:
            if current == ancestor_id:
                return True
            current = self._parent_map.get(current)
        return False

    def depth(self, agent_id: str) -> int:
        """Depth from root (root = 0)."""
        self.get(agent_id)
        d = 0
        current: str | None = agent_id
        while current is not None and self._parent_map.get(current) is not None:
            d += 1
            current = self._parent_map.get(current)
        return d

    def can_delegate_to(self, delegator_id: str, target_id: str) -> bool:
        """Check if delegator can delegate to target (must be direct child)."""
        if delegator_id not in self._agents or target_id not in self._agents:
            return False
        return target_id in self._children_map.get(delegator_id, [])

    def ancestors(self, agent_id: str) -> list[AgentConfig]:
        """All ancestors from parent up to root."""
        self.get(agent_id)
        result: list[AgentConfig] = []
        current = self._parent_map.get(agent_id)
        while current is not None:
            result.append(self._agents[current])
            current = self._parent_map.get(current)
        return result

    def subtree_ids(self, agent_id: str) -> list[str]:
        """All agent IDs in the subtree rooted at agent_id (including self)."""
        self.get(agent_id)
        result: list[str] = []
        stack = [agent_id]
        while stack:
            node = stack.pop()
            result.append(node)
            stack.extend(self._children_map.get(node, []))
        return result

    # -- Serialization ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize tree to a dict (list of agent configs)."""
        return {
            "agents": [agent.model_dump() for agent in self._agents.values()],
            "root_id": self._root_id,
        }

    @classmethod
    def from_config_file(cls, path: str | Path) -> AgentTree:
        """Load agent tree from a JSON configuration file.

        Expected format per SPEC §17:
        {
            "agents": [ { ... }, ... ]
        }
        """
        config_path = Path(path)
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path) as f:
            data = json.load(f)

        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AgentTree:
        """Load agent tree from a dictionary."""
        agents_data = data.get("agents", [])
        agents = [AgentConfig(**a) for a in agents_data]
        return cls(agents)
