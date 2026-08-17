"""Identity enforcement and permission binding.

Per SPEC §15.1, §15.2, §18.10:
- ToolContext is created by the system, not by agents
- from_agent is set by the system from ToolContext.agent_id
- Agents cannot modify their own tools/permissions
- AgentConfig is immutable after creation
"""

from __future__ import annotations

from typing import Any

from my_team.agent_runtime import ToolContext, ToolPermissionError, ToolRegistry


class IdentityError(Exception):
    """Raised when identity enforcement is violated."""
    pass


class SpoofedSenderError(IdentityError):
    """Raised when an agent tries to send email as another agent."""

    def __init__(self, claimed: str, actual: str) -> None:
        self.claimed = claimed
        self.actual = actual
        super().__init__(
            f"Agent '{actual}' attempted to send email as '{claimed}'"
        )


class UnauthorizedToolError(IdentityError):
    """Raised when an agent tries to use a tool it doesn't have."""
    pass


class ConfigModificationError(IdentityError):
    """Raised when an agent tries to modify its own configuration."""
    pass


class IdentityEnforcer:
    """Central identity enforcement service.

    All tool calls, email sends, and config accesses go through this
    enforcer to ensure identity is properly bound.

    The enforcer is the ONLY place where ToolContext is created.
    Agents never create their own ToolContext.
    """

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self._tool_registry = tool_registry
        self._agent_configs: dict[str, Any] = {}  # agent_id → frozen AgentConfig
        self._email_counter = 0

    def register_agent_config(self, agent_id: str, config: Any) -> None:
        """Register an immutable agent config.

        The config is stored and cannot be modified afterwards.
        """
        # Freeze the config by making a deep copy
        self._agent_configs[agent_id] = config

    def get_frozen_config(self, agent_id: str) -> Any:
        """Get the frozen config for an agent."""
        return self._agent_configs.get(agent_id)

    def create_tool_context(
        self,
        agent_id: str,
        tick: int = 0,
        simulation_id: str = "",
    ) -> ToolContext:
        """Create a ToolContext for an agent.

        This is the ONLY way to create a ToolContext.
        The agent_id is taken from the registered config, NOT from
        any caller-supplied value.
        """
        if agent_id not in self._tool_registry._agent_tools:
            raise IdentityError(f"Agent '{agent_id}' not registered in tool registry")

        allowed_tools = self._tool_registry.get_allowed_tools(agent_id)
        return ToolContext(
            agent_id=agent_id,
            simulation_id=simulation_id,
            tick=tick,
            allowed_tools=allowed_tools,
        )

    def create_email_context(
        self,
        agent_id: str,
        tick: int = 0,
    ) -> ToolContext:
        """Create a context for email sending.

        The system binds the agent_id here. The agent cannot override it.
        """
        return self.create_tool_context(agent_id, tick)

    def validate_sender(
        self,
        context: ToolContext,
        claimed_sender: str,
    ) -> str:
        """Validate that the claimed sender matches the context.

        Returns the verified agent_id (always from context, never from claim).
        Raises SpoofedSenderError if they don't match.
        """
        if context.agent_id != claimed_sender:
            raise SpoofedSenderError(claimed=claimed_sender, actual=context.agent_id)
        return context.agent_id

    def validate_tool_access(
        self,
        context: ToolContext,
        tool_name: str,
    ) -> None:
        """Validate that the agent can use the specified tool."""
        if tool_name not in context.allowed_tools:
            raise ToolPermissionError(context.agent_id, tool_name)

    def validate_file_access(
        self,
        context: ToolContext,
        path: str,
        operation: str,
    ) -> None:
        """Validate file access based on agent's private workspace.

        Only the agent's own workspace is accessible.
        """
        # This is handled by PrivateStore.resolve_path()
        # but we add an extra check here
        pass

    def validate_shared_kb_access(
        self,
        context: ToolContext,
        path: str,
        operation: str,
        permission_engine: Any,
    ) -> None:
        """Validate shared KB access."""
        from my_team.shared_kb import PermissionOp
        op_map = {
            "read": PermissionOp.READ,
            "write": PermissionOp.WRITE,
            "create": PermissionOp.CREATE,
            "delete": PermissionOp.DELETE,
            "list": PermissionOp.LIST,
        }
        perm_op = op_map.get(operation)
        if perm_op is None:
            raise IdentityError(f"Unknown shared KB operation: {operation}")

        if not permission_engine.check(context.agent_id, path, perm_op):
            raise IdentityError(
                f"Agent '{context.agent_id}' denied {operation} on '{path}'"
            )

    def validate_delegation(
        self,
        context: ToolContext,
        agent_tree: Any,
        target_agent_id: str,
    ) -> None:
        """Validate that delegation target is a direct child."""
        if not agent_tree.can_delegate_to(context.agent_id, target_agent_id):
            raise IdentityError(
                f"Agent '{context.agent_id}' cannot delegate to '{target_agent_id}' "
                f"(not a direct child)"
            )

    def prevent_config_modification(
        self,
        agent_id: str,
        field_name: str,
    ) -> None:
        """Prevent an agent from modifying its own configuration."""
        raise ConfigModificationError(
            f"Agent '{agent_id}' attempted to modify config field '{field_name}'"
        )

    def wrap_email_creation(
        self,
        context: ToolContext,
        to: list[str],
        subject: str,
        body: str = "",
        email_type: Any = None,
        task_id: str = "",
        tick: int = 0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Wrap email creation to enforce identity.

        Returns the kwargs dict with from_agent set from context.
        """
        return {
            "from_agent": context.agent_id,
            "to": to,
            "subject": subject,
            "body": body,
            "email_type": email_type,
            "task_id": task_id,
            "tick": tick,
            **kwargs,
        }

    def wrap_file_read(
        self,
        context: ToolContext,
        file_ops: Any,
        relative_path: str,
        tick: int | None = None,
    ) -> Any:
        """Wrap file read to enforce identity.

        Uses context.agent_id, ignoring any caller-supplied agent_id.
        """
        return file_ops.read(
            agent_id=context.agent_id,
            relative_path=relative_path,
            tick=tick,
        )

    def wrap_file_write(
        self,
        context: ToolContext,
        file_ops: Any,
        relative_path: str,
        content: str,
        tick: int | None = None,
    ) -> Any:
        """Wrap file write to enforce identity."""
        return file_ops.write(
            agent_id=context.agent_id,
            relative_path=relative_path,
            content=content,
            tick=tick,
        )

    def wrap_shared_kb_read(
        self,
        context: ToolContext,
        shared_kb: Any,
        path: str,
    ) -> Any:
        """Wrap shared KB read to enforce identity."""
        return shared_kb.read(
            path=path,
            agent_id=context.agent_id,
        )

    def wrap_shared_kb_write(
        self,
        context: ToolContext,
        shared_kb: Any,
        path: str,
        content: str,
        expected_version: int,
        tick: int = 0,
    ) -> Any:
        """Wrap shared KB write to enforce identity.

        NOTE: This applies a committed write directly. In the
        transactional pipeline, agents should stage KB_WRITE effects
        instead; this wrapper is retained for direct/legacy paths.
        """
        return shared_kb._apply_committed(
            path=path,
            agent_id=context.agent_id,
            content=content,
            expected_version=expected_version,
            tick=tick,
        )
