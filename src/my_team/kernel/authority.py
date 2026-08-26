"""Authority — 内核态设备：组织注册中心 + 认证系统（position + 多粒度 scope）。

权威移交：身份、position、能力与权限声明归 Authority 裁决登记；kernel 只
物化路由映射（identity → handle），不持有组织数据。

认证模型（Django 式，框架自带）：
- **grant 主体 = position**（非身份 uuid）：组织事实，换人不换岗。
- **多粒度 scope**：授权粒度是 `(position, device, token)`——token 为设备
  声明的不透明字符串（默认公开 / 页级只读 / 角色 / 类 api-key 凭证），
  语义由设备解释，Authority 只存不解释。安装时 `grants: [position...]`
  展开为设备的**默认公开** scope（设备级便捷）；运行期经 grant/revoke_scope
  系统命令细粒度调整。
- **Authority 自身 ACL**：命令面仅内核可调（source=system）；外部使用经
  kernel 系统命令（install/uninstall/grant/revoke），kernel 先查 Authority
  `authorize_request`——position 为 root（隐式全权）或持有 org 设备上对应
  org scope（人事权）。org scope 的授予仅 root 可做。
- **调用时认证（富化）**：kernel 路由设备事件时查 `auth_request`，把调用者
  (position, scopes) 附到事件上——设备据此按自己的语义裁决，无伪造面。

- 注入：工具条目（设备可见即注入）+ 已授 scope 的书面说明（type=skill
  条目）——"工具说明 + 技能记忆"作书面的使用与权限解释。
"""

from my_team.kernel.process import VOID, KernelModeDevice

ROOT_POSITION = "root"
ORG_DEVICE = "org"  # org scope 以 (position, org, scope) 授予

class Authority(KernelModeDevice):
    def __init__(self):
        super().__init__("authority")
        self._identities: dict[str, dict] = {}  # identity → {tools, scopes, agent, position}
        self._grants: dict[str, set[tuple]] = {}  # position → {(device, token)}
        self._injected: dict[str, dict] = {}    # agent → {name: entry}

    # ------------------------------------------------------------------
    # 命令面（kernel 专用；外部事件一律响亮拒绝）
    # ------------------------------------------------------------------

    async def respond(self, event):
        if event.get("source") != "system":
            return {"target": event["source"], "kind": "application",
                    "payload": {"command": "denied",
                                "reason": "Authority 命令面仅内核可调"}}
        command = event["payload"].get("command")
        if command == "register_request":
            payload = event["payload"]
            self._identities[payload["identity"]] = {
                "tools": payload.get("tools") or [],
                "scopes": payload.get("scopes") or [],
                "agent": bool(payload.get("agent")),
                "position": payload.get("position"),
            }
            return VOID
        if command == "unregister_request":
            identity = event["payload"]["identity"]
            self._identities.pop(identity, None)
            for grants in self._grants.values():
                for device, token in list(grants):
                    if device == identity:
                        grants.discard((device, token))
            return VOID
        if command == "grant_request":
            payload = event["payload"]
            self._grants.setdefault(payload["position"], set()).add(
                (payload["device"], payload["token"]))
            return VOID
        if command == "revoke_request":
            payload = event["payload"]
            self._grants.get(payload["position"], set()).discard(
                (payload["device"], payload["token"]))
            return VOID
        if command == "authorize_request":
            payload = event["payload"]
            return {"target": "kernel", "kind": "application",
                    "payload": {"allowed": self._authorized(
                        payload["identity"], payload["scope"])}}
        if command == "auth_request":
            return {"target": "kernel", "kind": "application",
                    "payload": {"auth": self._auth_context(
                        event["payload"]["identity"])}}
        if command == "inject_request":
            return self._build_inject(event["payload"]["agent"])
        if command == "agents_request":
            return {"target": "kernel", "kind": "application",
                    "payload": {"agents": [identity for identity, info in
                                           self._identities.items()
                                           if info["agent"]]}}
        return VOID

    # ------------------------------------------------------------------
    # 认证查询（kernel 用）
    # ------------------------------------------------------------------

    def _authorized(self, identity: str, scope: str) -> bool:
        """身份可否执行系统命令（安装/装卸/grant/revoke）。"""
        info = self._identities.get(identity)
        position = info["position"] if info else None
        if position == ROOT_POSITION:
            return True
        return bool(position and (ORG_DEVICE, scope)
                    in self._grants.get(position, set()))

    def _auth_context(self, identity: str) -> dict:
        """调用时认证上下文：调用者的 position 与其有效 scope 列表。"""
        info = self._identities.get(identity)
        position = info["position"] if info else None
        grants = self._grants.get(position) or set()
        return {"position": position,
                "scopes": [{"device": device, "token": token}
                           for device, token in sorted(grants)]}

    # ------------------------------------------------------------------
    # 注入（工具条目 + 已授 scope 书面说明）
    # ------------------------------------------------------------------

    def _build_inject(self, agent: str) -> dict:
        """汇总该 agent 可见条目：其 position 的 grants 覆盖设备的工具，
        以及已授 scope 的书面说明（type=skill，不参与 trigger 匹配）。

        per-agent 实例（identity 含 '@'，形如 <device-id>@<agent-id>）的
        条目只对绑定 agent 可见——注入隔离的权威在这里（_install 手动只
        注入绑定者只是减少无谓注入；全量重注入如卸载/scope 变更时本过滤
        兜底，防止按 position 布线泄露给其它 agent）。"""
        info = self._identities[agent]
        grants = self._grants.get(info["position"]) or set()
        by_device: dict[str, list[str]] = {}
        for device, token in grants:
            by_device.setdefault(device, []).append(token)

        def visible(dev_id: str) -> bool:
            """per-agent 实例只对绑定 agent 可见。"""
            return "@" not in dev_id or dev_id.split("@")[1] == agent

        new: dict[str, dict] = {}
        # 工具条目：按注册序聚合（确定性，同名后注册者胜）；布线只过滤
        for dev_id, dev in self._identities.items():
            if dev["agent"] or dev_id not in by_device or not visible(dev_id):
                continue
            for tool in dev["tools"]:
                new[tool["name"]] = self._entry(dev_id, tool)
        # skill 条目：已授且已声明的 scope 的书面说明（按设备名排序，确定性）
        for dev_id in sorted(by_device):
            dev = self._identities.get(dev_id)
            if dev is None or dev["agent"] or not visible(dev_id):
                continue
            for scope in dev.get("scopes") or []:
                if scope["token"] in by_device[dev_id]:
                    name = f"{dev_id}:{scope['token']}"
                    new[name] = self._perm_entry(dev_id, scope)
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

    @staticmethod
    def _perm_entry(device_id: str, scope: dict) -> dict:
        """scope 的书面说明条目（type=skill）：注入记忆作权限/用法解释。"""
        return {
            "entry_id": f"perm:{device_id}:{scope['token']}",
            "type": "skill",
            "content": {
                "name": f"{device_id}:{scope['token']}",
                "description": scope.get("explanation", ""),
                "parameters": {},
            },
            "trigger": [],
            "priority": 10,
            "associated": [device_id],
            "version": 1,
            "links": [],
            "deleted_at": None,
        }
