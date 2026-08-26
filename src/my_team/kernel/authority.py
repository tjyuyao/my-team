"""Authority — 内核态设备：组织注册中心（权威源）+ 系统能力注入。

权威移交：身份与能力声明归 Authority 裁决登记；kernel 只物化路由映射
（identity → handle），不持有组织数据。

- 注册：register_request（身份 + 工具定义声明 + agent 标志）→ 登记；
  unregister_request（身份）→ 撤销登记。
- 注入：inject_request（agent）→ 汇总各设备声明的工具条目，diff 旧注入
  → inject 事件（entries 新增/更新 + evict 移除名单），路由给 agent。
- agents_request → 当前全部 agent 身份（kernel 装卸设备时据此重注入）。
- 工具定义来自工作目录设备源码（数据化，随 install/uninstall 演化）。
- 第一版无 ACL/布线控制：全部设备能力注入给全部 agent（结构预留）。
"""

from my_team.kernel.process import VOID, KernelModeDevice


class Authority(KernelModeDevice):
    def __init__(self):
        super().__init__("authority")
        self._identities: dict[str, dict] = {}  # identity → {tools, agent}
        self._injected: dict[str, dict] = {}    # agent → {name: entry}

    async def respond(self, event):
        command = event["payload"].get("command")
        if command == "register_request":
            payload = event["payload"]
            self._identities[payload["identity"]] = {
                "tools": payload.get("tools") or [],
                "agent": bool(payload.get("agent")),
            }
            return VOID
        if command == "unregister_request":
            self._identities.pop(event["payload"]["identity"], None)
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
        """汇总该 agent 可见的工具条目（第一版 = 所有设备声明）。"""
        new: dict[str, dict] = {}
        for dev_id, info in self._identities.items():
            if info.get("agent"):
                continue
            for tool in info["tools"]:
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
