"""Agent：react 循环的认知主体（内心自持：记忆与决策都在进程内）。

模型：
- 状态 = messages（工作记忆，append-only）+ entries（精炼层，工具条目）。
- 工具 = 条目（Authority 注入，来自工作目录设备源码，数据化）：trigger
  匹配 → 查条目 → 按 associated 分发到设备 → tool_result 回来 → 产出
  agent_result。
- 决策（第一版演示）：任务内容对条目 trigger 做关键词匹配；LLM 决策
  未来接同一分发路径（查条目 → associated → 事件），决策函数可替换。
- 自举（bootstrap）：扫描自己的工作目录源码区（workdir/data/devices/*.py），
  对每个设备源码向内核发 install_device（grants 声明给自己的 position）——
  Root 从工作目录组装自己的能力；设备源码由 Root 生产（演示中为
  预置/落盘），文件即持久化形态。
"""

import os

from my_team.kernel.event_protocol import VOID
from my_team.kernel.process import UserModeProcess

AGENT_RESULT = "agent_result"
FILLERS = ("查询", "请问", "今天", "怎么样", "现在", "的", "呢", "？", "?", " ")


class Agent(UserModeProcess):
    def __init__(self, emit, *, workdir, position, needs_network=False):
        super().__init__(emit, 1)  # Agent 串行处理消息
        self.workdir = workdir
        self.position = position  # 布线主体（config options，与 Authority 一致）
        self.needs_network = needs_network  # 沙箱网络声明（进程级资源开关）
        self.messages = []  # 工作记忆（append-only）
        self.entries = {}  # 精炼层：name → 工具条目
        self._pending = None  # 自举任务（发起者 / 剩余回执数 / 错误）

    async def respond(self, event):
        command = event["payload"].get("command")
        if command == "inject":
            return self._on_inject(event["payload"])
        if command == "task":
            return self._on_task(event)
        if command == "tool_result":
            return self._on_tool_result(event)
        if command == "bootstrap":
            return self._on_bootstrap(event)
        if command in ("device_installed", "device_uninstalled"):
            return self._on_device_ack(event)
        return VOID

    # ---- 精炼层维护（Authority 注入） ----

    def _on_inject(self, payload):
        for name in payload.get("evict") or []:
            self.entries.pop(name, None)
        for entry in payload.get("entries") or []:
            self.entries[entry["content"]["name"]] = entry
        return VOID

    # ---- 自举：从工作目录组装能力 ----

    def _on_bootstrap(self, event):
        """扫描工作目录设备源码 → 逐个请求内核装载（grants 声明给自己的
        position——自举组装的能力归自己的岗；bound_agent 声明给自己——
        per-agent 设备实例绑定本 agent，命令落自己的家；shared 忽略）；
        全部回执后向发起者报告（agent_result）。无可装载时立即报告，
        不挂起；上轮未收齐时拒绝重入（防新旧回执混入同一计数器）。"""
        requester = event["source"]
        if self._pending is not None:
            return self._agent_result(requester, ok=False, error="上轮自举未完成")
        devices_dir = os.path.join(self.workdir, "data", "devices")
        files = []
        if os.path.isdir(devices_dir):
            files = sorted(f for f in os.listdir(devices_dir)
                           if f.endswith(".py") and not f.startswith("_"))
        self._pending = {"requester": requester, "remaining": len(files),
                         "errors": []}
        for filename in files:
            self.emit({
                "target": "kernel", "kind": "system",
                "payload": {"command": "install_device",
                            "identity": filename[:-3],
                            "source_file": os.path.join(devices_dir, filename),
                            "grants": [self.position],
                            "bound_agent": self.identity},
            })
        if not files:
            self._pending = None
            return self._agent_result(requester, ok=True, content="无可装载")
        return VOID

    def _on_device_ack(self, event):
        """内核回执（install/uninstall）：聚合 pending 自举任务，收齐后
        向发起者报告结果。错误来自回执的 ok 标志，errors 即唯一真相。"""
        pending = self._pending
        if pending is None:
            return VOID
        payload = event["payload"]
        pending["remaining"] -= 1
        if not payload.get("ok"):
            pending["errors"].append(
                f"{payload.get('identity')}: {payload.get('error')}")
        if pending["remaining"] > 0:
            return VOID
        requester = pending["requester"]
        errors = pending["errors"]
        self._pending = None
        return self._agent_result(
            requester, ok=not errors,
            content="设备装载完成" if not errors
            else "装载失败：" + "; ".join(errors))

    # ---- react 决策 ----

    def _on_task(self, event):
        content = event["payload"].get("content", "")
        self.messages.append({"role": "user", "content": content,
                              "source": event["source"]})
        entry = self._match(content)
        if entry is None:
            return self._agent_result(event["source"], ok=False, error="没有匹配的工具")
        arguments = event["payload"].get("arguments") or self._extract_args(entry, content)
        return {
            "target": entry["associated"][0],
            "kind": "application",
            "payload": {"command": "tool_run", "name": entry["content"]["name"],
                        "arguments": arguments, "task": event["source"]},
        }

    def _on_tool_result(self, event):
        payload = event["payload"]
        return self._agent_result(payload.get("task"), ok=payload.get("ok"),
                                  content=payload.get("content"),
                                  error=payload.get("error"))

    def _agent_result(self, requester, *, ok, content=None, error=None):
        return {"target": requester, "kind": "application",
                "payload": {"command": AGENT_RESULT, "ok": ok,
                            "content": content, "error": error}}

    # ---- 决策细节（第一版演示：trigger 数据化匹配） ----

    def _match(self, content):
        """任务内容对条目 trigger 做子串匹配（数据驱动，无硬编码工具名）。"""
        for entry in self.entries.values():
            for trigger in entry.get("trigger") or []:
                if trigger and trigger in content:
                    return entry
        return None

    def _extract_args(self, entry, content):
        """演示级参数抽取：剥掉 trigger 与填充词，剩余为参数值。"""
        stripped = content
        for trigger in entry.get("trigger") or []:
            stripped = stripped.replace(trigger, "")
        for word in FILLERS:
            stripped = stripped.replace(word, "")
        stripped = stripped.strip()
        return {"city": stripped} if stripped else {}
