"""Agent：react 循环的认知主体（内心自持：记忆与决策都在进程内）。

模型：
- 状态 = messages（工作记忆，append-only）+ entries（精炼层，工具条目）。
- 工具 = 条目（Authority 注入，来自 team 配置，数据化）：trigger 匹配 →
  查条目 → 按 associated 分发到设备 → tool_result 回来 → 产出 agent_result。
- 决策（第一版演示）：任务内容对条目 trigger 做关键词匹配；LLM 决策
  未来接同一分发路径（查条目 → associated → 事件），决策函数可替换。
- 热加载：inject/evict 事件维护条目（工具集合随配置演化）。
"""

from my_team.kernel.event_protocol import VOID
from my_team.kernel.process import UserModeProcess

AGENT_RESULT = "agent_result"
FILLERS = ("查询", "请问", "今天", "怎么样", "现在", "的", "呢", "？", "?", " ")


class Agent(UserModeProcess):
    def __init__(self, emit):
        super().__init__(emit, 1)  # Agent 串行处理消息
        self.messages = []  # 工作记忆（append-only）
        self.entries = {}  # 精炼层：name → 工具条目

    async def respond(self, event):
        command = event["payload"].get("command")
        if command == "inject":
            return self._on_inject(event["payload"])
        if command == "task":
            return self._on_task(event)
        if command == "tool_result":
            return self._on_tool_result(event)
        return VOID

    # ---- 精炼层维护（Authority 注入 / 热加载） ----

    def _on_inject(self, payload):
        for name in payload.get("evict") or []:
            self.entries.pop(name, None)
        for entry in payload.get("entries") or []:
            self.entries[entry["content"]["name"]] = entry
        return VOID

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
