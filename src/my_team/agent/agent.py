"""Agent：react 循环的认知主体（内心自持：记忆与决策都在进程内）。

模型：
- 唯一状态 = memory（append-only 记忆条目，事件即条目）。
- 事件到来 → 反应：事件入记忆 → 决策 → 产出事件。永远如此。
- 无会话/对话/requester 概念：上下文与回填目标都从记忆恢复。
- 工作记忆映射：决策时把记忆映射为 wire 消息发给 LLM 设备。
- 工具调用串行执行：llm_result 带多个 tool_calls 时逐个发出，
  下一个由"记忆推断"（比较 llm_result 条目与已执行的 bash_result）触发。
"""

from __future__ import annotations

from uuid import uuid4

from my_team.device.llm import LLM_REQUEST, LLM_RESULT
from my_team.kernel.process import Process

BASH_TOOL = {
    "name": "bash",
    "description": "执行 shell 命令，返回输出与退出码。",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}

AGENT_RESULT = "agent_result"
MAX_MESSAGES = 30  # 工作记忆映射上限（防止记忆无限膨胀）


class Agent(Process):
    def __init__(self, emit, llm_pid, bash_pid, *, model):
        super().__init__(emit)
        self.llm_pid = llm_pid
        self.bash_pid = bash_pid
        self.model = model
        self.memory = []  # append-only 记忆条目

    # ---- react 循环 ----

    def respond(self, event):
        self._remember(event)
        return self._react(event)

    def _remember(self, event):
        self.memory.append({
            "entry_id": str(uuid4()),
            "source": event["source"],
            "payload": event["payload"],
        })

    def _react(self, event):
        command = event["payload"].get("command")
        if command == LLM_RESULT:
            return self._on_llm_result()
        if command == "bash_result":
            return self._on_tool_result()
        return self._ask_llm()  # 触发事件：开始工作

    # ---- 决策：工作记忆 → LLM ----

    def _ask_llm(self):
        return {
            "target": self.llm_pid,
            "kind": "application",
            "payload": {
                "command": LLM_REQUEST,
                "model": self.model,
                "system": "你是工作 Agent。需要执行命令时调用 bash 工具，"
                          "拿到结果后继续，直到完成任务。",
                "messages": self._map_messages(),
                "tools": [BASH_TOOL],
            },
        }

    def _map_messages(self):
        """工作记忆映射：记忆条目 → wire 消息（最近 MAX_MESSAGES 条）。"""
        messages = []
        for entry in self.memory[-MAX_MESSAGES:]:
            payload = entry["payload"]
            command = payload.get("command")
            if command == LLM_RESULT:
                messages.append({
                    "role": "assistant",
                    "content": payload.get("content") or "",
                    "tool_calls": payload.get("tool_calls") or [],
                })
            elif command == "bash_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": payload.get("tool_call_id") or "",
                    "content": payload.get("content") or "",
                    "is_error": not payload.get("ok"),
                })
            else:  # 触发事件：作为 user 消息
                messages.append({"role": "user", "content": payload.get("content", "")})
        return messages

    # ---- 分支 ----

    def _on_llm_result(self):
        last = self.memory[-1]["payload"]
        if not last.get("ok"):
            return self._finish(ok=False, error=last.get("error") or "llm failed")
        tool_calls = last.get("tool_calls") or []
        if not tool_calls:
            return self._finish(ok=True, content=last.get("content") or "")
        return self._bash_run(tool_calls[0])  # 串行：先发第一个

    def _on_tool_result(self):
        pending = self._pending_tool_calls()
        if pending:
            return self._bash_run(pending[0])
        return self._ask_llm()  # 全部执行完，回填 LLM 继续决策

    # ---- 记忆推断 ----

    def _pending_tool_calls(self):
        """最近的 llm_result 条目中尚未执行的 tool_calls。"""
        done = {
            e["payload"].get("tool_call_id") for e in self.memory
            if e["payload"].get("command") == "bash_result"
        }
        for entry in reversed(self.memory):
            payload = entry["payload"]
            if payload.get("command") == LLM_RESULT:
                return [c for c in (payload.get("tool_calls") or [])
                        if c.get("id") not in done]
        return []

    def _work_start_source(self):
        """工作起点：记忆中第一个非设备产出条目的来源（回填 target）。"""
        for entry in self.memory:
            if entry["payload"].get("command") not in (LLM_RESULT, "bash_result"):
                return entry["source"]
        return self.memory[0]["source"] if self.memory else None

    # ---- 产出 ----

    def _bash_run(self, tool_call):
        return {
            "target": self.bash_pid,
            "kind": "application",
            "payload": {
                "command": "bash_run",
                "cmd": (tool_call.get("arguments") or {}).get("command", ""),
                "tool_call_id": tool_call.get("id"),
            },
        }

    def _finish(self, *, ok, content=None, error=None):
        requester = self._work_start_source()
        return {
            "target": requester,
            "kind": "application",
            "payload": {
                "command": AGENT_RESULT,
                "ok": ok,
                "content": content,
                "error": error,
            },
        }
