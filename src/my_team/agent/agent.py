"""Agent：react 循环的认知主体（内心自持：记忆与决策都在进程内）。

模型：
- 状态 = messages（工作记忆，append-only）+ memory（精炼层，条目组织）。
  事件 append 到 messages（为 input cache rate）；决策时从精炼层
  召回的知识增量 append 到 messages 末尾。
- 事件到来 → 反应：事件转消息 append → 决策 → 产出事件。永远如此。
- 无会话/对话/requester 概念：发起者从 messages 条目恢复（触发条目
  带 source 元数据，LLM 设备协议忽略未知字段）。
- 工具 = 精炼层的 type=tool 条目（设备记忆注入的结果）：tools= 每次
  从条目动态生成；tool_call 按条目 associated 分发到设备。
- 一次只执行一个工具调用。
- 整理模式（CONSOLIDATING）：messages 超预算或收到整理意图时进入
  整理回合——tools 收窄为记忆工具集（memory_fold），多轮工具调用
  在本地执行，直到 LLM 不再调用工具，整理完成。
"""

from __future__ import annotations

from uuid import uuid4

from my_team.device.llm import LLM_REQUEST, LLM_RESULT
from my_team.kernel.process import Process

MEMORY_FOLD_TOOL = {
    "name": "memory_fold",
    "description": "把历史折叠为一段结构化摘要（保留任务目标、已完成动作、"
                    "关键结论与待办）。整理完成后不要再调用工具。",
    "parameters": {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "折叠后的摘要"}},
        "required": ["summary"],
    },
}

WORK_SYSTEM = ("你是工作 Agent。需要执行命令时调用 bash 工具，"
               "拿到结果后继续，直到完成任务。")
CONSOLIDATE_SYSTEM = ("你是整理者。把当前历史折叠为一段结构化摘要（memory_fold），"
                      "摘要须保留任务目标、已完成动作、关键结论与待办。"
                      "整理完成后不再调用工具。")

AGENT_RESULT = "agent_result"
CONSOLIDATED = "consolidated"
MAX_MESSAGES = 30  # 预算阈值：超过即进入整理回合


class Agent(Process):
    def __init__(self, emit, llm_pid, model, *, seed_tools=None):
        super().__init__(emit, 1)  # Agent 串行处理消息
        self.llm_pid = llm_pid
        self.model = model
        self.messages = []  # 工作记忆（append-only）
        self.consolidating = False  # 整理回合标志
        self.memory = [self._tool_entry(t) for t in (seed_tools or [])]  # 精炼层

    # ---- react 循环 ----

    async def respond(self, event):
        self._append(event)
        return self._react(event)

    def _append(self, event):
        """事件 → wire 消息 append。触发条目附带 source 元数据（恢复发起者）。"""
        payload = event["payload"]
        command = payload.get("command")
        if command == LLM_RESULT:
            self.messages.append({
                "role": "assistant",
                "content": payload.get("content") or "",
                "tool_calls": payload.get("tool_calls") or [],
                "ok": payload.get("ok"),
                "error": payload.get("error"),
            })
        elif command == "bash_result":
            self.messages.append({
                "role": "tool",
                "tool_call_id": payload.get("tool_call_id") or "",
                "content": payload.get("content") or "",
                "is_error": not payload.get("ok"),
            })
        else:  # 触发事件
            self.messages.append({
                "role": "user",
                "content": payload.get("content", ""),
                "source": event["source"],
            })

    def _react(self, event):
        command = event["payload"].get("command")
        if command == LLM_RESULT:
            return self._on_llm_result()
        if command == "bash_result":
            return self._on_tool_result()
        if command == "consolidate":
            self.consolidating = True
        return self._ask_llm()

    # ---- 精炼层：工具条目 ----

    def _tool_entry(self, tool):
        return {
            "entry_id": str(uuid4()),
            "type": "tool",
            "content": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
            "trigger": list(tool.get("trigger") or []),
            "priority": tool.get("priority", 10),
            "associated": list(tool["associated"]),
            "version": 1,
            "links": [],
            "deleted_at": None,
        }

    def _tools_for_llm(self):
        """tools= 从工具条目动态生成（未删除的 type=tool 条目）。"""
        return [e["content"] for e in self.memory
                if e["type"] == "tool" and e.get("deleted_at") is None]

    def _dispatch(self, tool_call):
        """tool_call.name → 查工具条目 → associated 设备 → 构造该设备协议事件。"""
        name = tool_call.get("name")
        entry = next(
            (e for e in self.memory
             if e["type"] == "tool" and e["content"]["name"] == name
             and e.get("deleted_at") is None),
            None,
        )
        if entry is None:
            return {"target": "void", "kind": "application",
                    "payload": {"command": "unknown_tool", "name": name}}
        device_pid = entry["associated"][0]
        return {
            "target": device_pid,
            "kind": "application",
            "payload": {
                "command": "bash_run",  # 设备协议事件，由设备 PROTOCOL 定义
                "cmd": (tool_call.get("arguments") or {}).get("command", ""),
                "tool_call_id": tool_call.get("id"),
            },
        }

    # ---- 决策 ----

    def _ask_llm(self):
        if not self.consolidating and len(self.messages) > MAX_MESSAGES:
            self.consolidating = True  # 预算触发整理回合
        return {
            "target": self.llm_pid,
            "kind": "application",
            "payload": {
                "command": LLM_REQUEST,
                "model": self.model,
                "system": CONSOLIDATE_SYSTEM if self.consolidating else WORK_SYSTEM,
                "messages": self.messages[-MAX_MESSAGES:],
                "tools": [MEMORY_FOLD_TOOL] if self.consolidating else self._tools_for_llm(),
            },
        }

    def _on_llm_result(self):
        last = self.messages[-1]
        if not last.get("ok"):
            self.consolidating = False
            return self._finish(ok=False, error=last.get("error") or "llm failed")
        tool_calls = last.get("tool_calls") or []
        if not tool_calls:
            if self.consolidating:
                self.consolidating = False  # 整理回合完成
                return {"target": "void", "kind": "application",
                        "payload": {"command": CONSOLIDATED}}
            return self._finish(ok=True, content=last.get("content") or "")
        call = tool_calls[0]  # 一次一个工具调用
        if call.get("name") == "memory_fold":
            return self._apply_fold(call)
        return self._dispatch(call)

    def _on_tool_result(self):
        return self._ask_llm()  # 工具结果回填 messages，继续决策

    # ---- 记忆工具（本地执行） ----

    def _apply_fold(self, call):
        """memory_fold：历史折叠为摘要，替换 messages。"""
        summary = (call.get("arguments") or {}).get("summary", "") or "(folded)"
        requester = self._work_start_source()
        folded = len(self.messages)
        self.messages = [{"role": "user", "content": summary, "source": requester}]
        self.messages.append({
            "role": "tool",
            "tool_call_id": call.get("id"),
            "content": f"已折叠 {folded} 条历史为摘要。",
        })
        return self._ask_llm()  # 继续整理回合

    # ---- 产出 ----

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

    def _work_start_source(self):
        """发起者：最近的触发条目（user 消息带 source 元数据）。"""
        for message in reversed(self.messages):
            if message.get("source") is not None:
                return message["source"]
        return None
