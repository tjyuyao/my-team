"""LLM 设备进程：为其他进程提供 LLM API 能力（vendor 协议层）。

事件协议（application 层，payload.command 路由）：
- llm_request：{command, model, system?, messages, tools?, options?}
  messages 为 wire 格式列表：
  - {role: "user", content}
  - {role: "assistant", content?, tool_calls?: [{id, name, arguments}]}
  - {role: "tool", tool_call_id, content, is_error?}
  tools 为 [{name, description?, parameters?}]
- llm_result：{command, ok, content, tool_calls?, usage?, error?}
  target 回填请求方 pid。

设备串行处理请求：一次一个 in-flight，其余在 inbox 排队（排队不拒绝）。
provider 由工厂在进程内懒创建（httpx 客户端不可跨进程 pickle）。
"""

import asyncio

from my_team.device.llm.vendor.types.messages import (
    AssistantMessage,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    assistant_content,
)
from my_team.device.llm.vendor.types.provider_events import (
    AssistantDoneEvent,
    AssistantErrorEvent,
)
from my_team.device.llm.vendor.types.tools import AgentTool
from my_team.kernel.process import Process

LLM_REQUEST = "llm_request"
LLM_RESULT = "llm_result"


class LLMDevice(Process):
    """LLM 服务进程：收 llm_request 事件，调用 provider，产出 llm_result。"""

    def __init__(self, emit, provider_factory, poll_interval=0.05):
        super().__init__(emit, poll_interval=poll_interval)
        self.provider_factory = provider_factory
        self._provider = None

    def respond(self, event: dict) -> dict:
        if self._provider is None:
            self._provider = self.provider_factory()
        result = asyncio.run(self._call(self._provider, event["payload"]))
        return {"target": event["source"], "kind": "application", "payload": result}

    async def _call(self, provider, payload: dict) -> dict:
        try:
            result = None
            async for ev in provider.stream_response(
                model=payload["model"],
                system=payload.get("system") or "",
                messages=_messages_from_wire(payload.get("messages") or []),
                tools=_tools_from_wire(payload.get("tools") or []),
            ):
                if isinstance(ev, AssistantDoneEvent):
                    result = _result_ok(ev.message)
                    break
                if isinstance(ev, AssistantErrorEvent):
                    result = _result_error(ev.error.text or "provider error")
                    break
            return result or _result_error("stream ended without a terminal event")
        except Exception as exc:
            return _result_error(str(exc))


def _result_ok(message: AssistantMessage) -> dict:
    tool_calls = [
        {"id": t.id, "name": t.name, "arguments": t.arguments} for t in message.tool_calls
    ]
    return {
        "command": LLM_RESULT,
        "ok": True,
        "content": message.text,
        "tool_calls": tool_calls,
        "usage": message.usage.model_dump() if message.usage is not None else None,
    }


def _result_error(error: str) -> dict:
    return {"command": LLM_RESULT, "ok": False, "content": "", "error": error}


def _messages_from_wire(items: list[dict]) -> list:
    messages = []
    for item in items:
        role = item.get("role")
        if role == "user":
            messages.append(UserMessage(content=item.get("content", "")))
        elif role == "assistant":
            tool_calls = [
                ToolCall(id=t["id"], name=t["name"], arguments=t.get("arguments") or {})
                for t in item.get("tool_calls") or []
            ]
            messages.append(
                AssistantMessage(
                    content=assistant_content(item.get("content", ""), tool_calls)
                )
            )
        elif role == "tool":
            messages.append(
                ToolResultMessage(
                    tool_call_id=item["tool_call_id"],
                    tool_name=item.get("tool_name", ""),
                    content=[TextContent(text=item.get("content", ""))],
                    is_error=bool(item.get("is_error")),
                )
            )
    return messages


def _tools_from_wire(items: list[dict]) -> list[AgentTool]:
    # 设备不执行工具（执行归 agent），execute_fn 仅占位。
    async def _never_execute(*_args, **_kwargs):
        raise RuntimeError("LLM device never executes tools")

    return [
        AgentTool(
            name=t["name"],
            label=t["name"],
            description=t.get("description", ""),
            parameters=t.get("parameters") or {},
            execute_fn=_never_execute,
        )
        for t in items
    ]
