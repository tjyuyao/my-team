"""Authority — 内核态设备：组织注册中心（权威源）+ 布线控制 + 系统能力注入。

权威移交：身份、position 与能力声明归 Authority 裁决登记；kernel 只物化
路由映射（identity → handle），不持有组织数据。

- 注册：register_request（身份 + 工具定义声明 + agent 标志 + position）
  → 登记；unregister_request（身份）→ 撤销登记并连带撤销其全部布线。
- 布线（grant 表）：grant_request（position, entity）→ 登记可见性。
  **deny-by-default**：注入内容 = agent 的 position 所布线设备的声明；
  未布线的设备能力对任何 agent 不可见。
- 注入：inject_request（agent）→ 按布线汇总工具条目，diff 旧注入
  → inject 事件（entries 新增/更新 + evict 移除名单），路由给 agent。
- agents_request → 当前全部 agent 身份（kernel 装卸设备时据此重注入）。
- 工具定义来自工作目录设备源码（数据化，随 install/uninstall 演化）。

开放问题与演进方向见 AUTHORITY.md（同目录）。
"""

from my_team.kernel.process import VOID, KernelModeDevice


class Authority(KernelModeDevice):
    def __init__(self):
        super().__init__("authority")
        self._identities: dict[str, dict] = {}  # identity → {tools, agent, position}
        self._grants: dict[str, set[str]] = {}  # position → {device identity}
        self._injected: dict[str, dict] = {}    # agent → {name: entry}

    async def respond(self, event):
        command = event["payload"].get("command")
        if command == "register_request":
            payload = event["payload"]
            self._identities[payload["identity"]] = {
                "tools": payload.get("tools") or [],
                "agent": bool(payload.get("agent")),
                "position": payload.get("position"),
            }
            return VOID
        if command == "unregister_request":
            identity = event["payload"]["identity"]
            self._identities.pop(identity, None)
            for position in self._grants.values():
                position.discard(identity)  # 卸载连带撤销布线
            return VOID
        if command == "grant_request":
            payload = event["payload"]
            self._grants.setdefault(payload["position"], set()).add(
                payload["entity"])
            return VOID
        if command == "inject_request":
            return self._build_inject(event["payload"]["agent"])
        if command == "agents_request":
            return {"target": "kernel", "kind": "application",
                    "payload": {"agents": [identity for identity, info in
                                           self._identities.items()
                                           if info["agent"]]}}
        return VOID

    def _build_inject(self, agent: str) -> dict:
        """汇总该 agent 可见的工具条目（= 其 position 所布线的设备声明）。"""
        info = self._identities[agent]
        visible = self._grants.get(info["position"]) or set()
        new: dict[str, dict] = {}
        # 按注册序聚合（确定性）：同名工具后注册者胜；布线只过滤，不改序
        for dev_id, dev in self._identities.items():
            if dev_id not in visible or dev["agent"]:
                continue
            for tool in dev["tools"]:
                new[tool["name"]] = self._entry(dev_id, tool)
        old = self._injected.get(agent, {})
        evict = [name for name in old if name not in new]
        self._injected[agent] = new
        return {
            "target": agent,
            "kind": "application",
            "payload": {
                "command": "inject",
                "entries": list(new.values()),
                "evict": evict,
            },
        }

    @staticmethod
    def _entry(device_id: str, tool: dict) -> dict:
        # entry_id 稳定：工具定义未变则身份不变（条目身份是引用锚点，
        # 避免注入 churn 腐蚀 links/associated）。
        return {
            "entry_id": f"tool:{device_id}:{tool['name']}",
            "type": "tool",
            "content": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters", {}),
            },
            "trigger": list(tool.get("trigger") or []),
            "priority": tool.get("priority", 10),
            "associated": [device_id],
            "version": 1,
            "links": [],
            "deleted_at": None,
        }
