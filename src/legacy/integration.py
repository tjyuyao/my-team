"""Integration registry and provider-level admission (T9, v0.10 边界).

Per the ingress/egress transport-layer card:

- ``Integration`` is the first-class description of an external platform
  adapter (SPEC §6.4). The kernel treats only the Integration contract,
  never a concrete platform.
- ``ProviderAdmission`` is a SEPARATE rate-limiting gate from executor
  admission (decision 1b). Executor admission (executor_registry.py)
  governs the kernel side — who runs a tool and how much concurrent
  capacity. Provider admission governs the EXTERNAL platform side —
  how many calls per window THAT platform will accept. The two are
  different ownership dimensions and must never be merged into a single
  function; an outbound op is dispatched only when BOTH admit
  (``executor_admitted AND provider_admitted``).
- The ``external_id <-> op_id`` translation (decision 4) is platform-
  specific and belongs to plugin/scenario-pack code, NOT the kernel.
  Each Integration declares a *receipt assertion* describing which field
  of an ingress receipt yields the platform's external id; consuming the
  receipt into a PendingOperation is the kernel's job, but translating
  that external id into an op is done by a plugin-provided mapping
  function exposed through the assertion.

N1c-3 设备归位：IntegrationRegistry 继承 Device，注册外部集成数据面
受控实体（rate_limits/health 等外部资源限额归属此设备）；admit/
record_dispatched 保持原接口（executor ∧ provider 放行语义不变）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from my_team.devices.base import Device, EntityKind, InjectionDecl
from my_team.tool_manifest import ToolManifest
from pydantic import BaseModel, Field


class ReceiptAssertion(BaseModel):
    """Kernel-defined extension interface for inbound platform receipts.

    A real platform's receipt never carries the kernel op_id; it carries
    the platform's own external_id (comment id, order id, ...). The
    translation `external_id -> op_id` is platform-specific, so the
    kernel defines only the CONTRACT:

    - ``external_id_field``: which key in the Ingress payload holds the
      platform's external id.
    - ``op_id_resolver``: a plugin-supplied callable that, given the
      external id and the integration snapshot, returns the kernel op_id
      (a ``str``) or ``None`` when it cannot resolve.

    The resolver implementation is plugin/scenario-pack code (decision 4);
    the kernel only validates the assertion shape and calls the resolver.
    """

    external_id_field: str = Field(
        description="Receipt payload key holding the platform's external id",
    )
    op_id_resolver: Callable[[str, Mapping[str, Any]], str | None] = Field(
        description="external_id -> kernel op_id; None if unresolvable",
    )


class RateLimit(BaseModel):
    """Per-Integration outbound rate limit (external platform quota)."""

    max_calls: int = Field(ge=0, description="Max calls in the window")
    window_seconds: float = Field(
        gt=0,
        description="Window length in wall-clock seconds",
    )


class Integration(BaseModel):
    """External platform adapter registration (SPEC §6.4)."""

    name: str = Field(description="Unique integration / platform name")
    credential_ref: str = Field(
        default="",
        description="Credential via CredentialStore (never in journal)",
    )
    rate_limits: RateLimit = Field(
        default_factory=lambda: RateLimit(max_calls=100, window_seconds=60),
        description="Outbound quota enforced by ProviderAdmission",
    )
    manifests: list[ToolManifest] = Field(
        default_factory=list,
        description="Outbound tool manifests registered on registration "
                    "(dynamic registration per decision 2)",
    )
    ingress_event_types: list[str] = Field(
        default_factory=list,
        description="Ingress event types this platform may emit",
    )
    health_check: str = Field(
        default="",
        description="Health check probe identifier",
    )
    receipt: ReceiptAssertion | None = Field(
        default=None,
        description="Receipt assertion (external_id -> op_id translation)",
    )


@dataclass
class _WindowState:
    """Sliding-window rate-limit bookkeeping for a single integration."""

    window_started_at: float = field(default_factory=time.monotonic)
    calls_in_window: int = 0


class IntegrationRegistry(Device):
    """外部集成注册表 + 平台级 Admission（N1c-3 设备归位，SPEC §5.11）。

    - 数据面（此设备持有）：Integration 注册信息、rate_limits（外部资源
      限额）、health_check 标识——外部资源限额归属此设备（§5.11）；
    - 行为面（内核保留）：`admit()/record_dispatched` 放行语义
      ``executor ∧ provider`` 不变，健康背压通过 provider 层 Admission；
    - 继承 Device 后注册范围级 DATA 实体（集成数据归位）。

    Registered integrations; the kernel's single view of external platforms.
    """

    def __init__(self, device_id: str | None = None) -> None:
        # Device 基类初始化（注册受控实体）
        Device.__init__(self, device_id)
        self._integrations: dict[str, Integration] = {}
        self._providers_by_tool: dict[str, str] = {}
        self._windows: dict[str, _WindowState] = {}
        # N1c-3：注册外部集成数据面受控实体（范围级 DATA）
        self.integration_scope_id = self.register_entity(
            EntityKind.DATA,
            "integration-scope",
            injection=InjectionDecl(
                content=(
                    "[INTEGRATION_INSTRUCTION] 集成设备（IntegrationDevice）"
                    "持有外部平台适配器注册信息。\n"
                    "外部资源速率限额（rate_limits）与健康检查标识（health_check）"
                    "归属此设备数据面（§5.11）。\n"
                    "平台级 Admission（provider 层）与执行器 Admission（内核层）"
                    "是两个独立放行门，须同时通过才能分发（executor ∧ provider）。"
                ),
                source_tag="[INTEGRATION_INSTRUCTION]",
            ),
        )

    def register(self, integration: Integration) -> None:
        """Register (or replace) an integration and index its outbound tools.

        Dynamic outbound-tool registration (decision 2): each manifest in
        ``integration.manifests`` is indexed so the kernel can resolve
        which provider "owns" a given outbound tool.

        Raises ValueError on an ambiguous tool ownership (a tool name
        claimed by more than one integration) so dynamic registration
        never silently routes a tool to the wrong platform.
        """
        if integration.name in self._integrations:
            # replace but keep existing window state (rate limit continuity)
            pass
        self._integrations[integration.name] = integration
        for manifest in integration.manifests:
            owner = self._providers_by_tool.get(manifest.name)
            if owner is not None and owner != integration.name:
                raise ValueError(
                    f"Outbound tool '{manifest.name}' is claimed by both "
                    f"'{owner}' and '{integration.name}'"
                )
            self._providers_by_tool[manifest.name] = integration.name

    def unregister(self, name: str) -> None:
        integration = self._integrations.pop(name, None)
        self._windows.pop(name, None)
        if integration is not None:
            for manifest in integration.manifests:
                if self._providers_by_tool.get(manifest.name) == name:
                    del self._providers_by_tool[manifest.name]

    def get(self, name: str) -> Integration | None:
        return self._integrations.get(name)

    def get_by_tool(self, tool_name: str) -> Integration | None:
        provider = self._providers_by_tool.get(tool_name)
        if provider is None:
            return None
        return self._integrations.get(provider)

    def all(self) -> tuple[Integration, ...]:
        return tuple(self._integrations.values())

    # -- provider admission --------------------------------------------------

    def admit(self, tool_name: str) -> tuple[bool, str, bool]:
        """Provider-level admission for an outbound tool.

        Returns ``(admitted, reason, retryable)`` mirroring the executor
        admission tuple so dispatch can treat the two gates uniformly:
        - unknown provider -> permanent denial (retryable=False): a tool
          with no owning integration is a config error.
        - quota exhausted -> retryable=True: keep the op SUBMITTED
          (backpressure), re-admit next tick.
        """
        integration = self.get_by_tool(tool_name)
        if integration is None:
            return (
                False,
                f"No integration owns outbound tool '{tool_name}'",
                False,
            )
        limit = integration.rate_limits
        state = self._windows.setdefault(
            integration.name, _WindowState(),
        )
        now = time.monotonic()
        if now - state.window_started_at >= limit.window_seconds:
            state.window_started_at = now
            state.calls_in_window = 0
        if state.calls_in_window >= limit.max_calls:
            return (
                False,
                f"Integration '{integration.name}' rate limit reached "
                f"({limit.max_calls}/{limit.window_seconds:.0f}s)",
                True,
            )
        return (True, "", False)

    def record_dispatched(self, tool_name: str) -> None:
        """Charge one call against the owning provider's window.

        Called by dispatch once a provider-admitted op is actually
        dispatched to the external platform.
        """
        integration = self.get_by_tool(tool_name)
        if integration is None:
            return
        state = self._windows.setdefault(integration.name, _WindowState())
        now = time.monotonic()
        if now - state.window_started_at >= integration.rate_limits.window_seconds:
            state.window_started_at = now
            state.calls_in_window = 0
        state.calls_in_window += 1

    def resolve_op_id(
        self,
        tool_or_provider: str,
        external_id: str,
        ingress_payload: Mapping[str, Any],
    ) -> str | None:
        """Resolve an ingress receipt's external_id to a kernel op_id.

        Decision 4: translation is plugin code. The kernel locates the
        owning integration's ReceiptAssertion and delegates to its
        ``op_id_resolver``. Returns None when no integration/assertion
        is present or the plugin cannot resolve.
        """
        integration = self.get_by_provider_name(tool_or_provider)
        if integration is None or integration.receipt is None:
            return None
        return integration.receipt.op_id_resolver(external_id, ingress_payload)

    # -- introspection -------------------------------------------------------

    def get_by_provider_name(self, name: str) -> Integration | None:
        return self._integrations.get(name)

    def provider_for_tool(self, tool_name: str) -> str | None:
        return self._providers_by_tool.get(tool_name)

    def snapshot(self) -> dict[str, Any]:
        return {
            "integrations": {
                n: {
                    "name": i.name,
                    "ingress_event_types": list(i.ingress_event_types),
                    "outbound_tools": [m.name for m in i.manifests],
                    "receipt": (
                        i.receipt.external_id_field if i.receipt else None
                    ),
                }
                for n, i in self._integrations.items()
            },
            "providers_by_tool": dict(self._providers_by_tool),
        }
