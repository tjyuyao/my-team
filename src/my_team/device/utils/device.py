"""Utils 设备（用户态）：通用工具执行设备。

承接 tool_run（name + arguments）→ 按名路由到处理器 → tool_result。
工具定义（name/description/parameters/trigger）由 team 配置声明并经
Authority 注入 agent；本设备只实现命名处理器——能力在代码，暴露在配置。
"""

import time

from my_team.kernel.process import VOID, UserModeProcess


class UtilsDevice(UserModeProcess):
    def __init__(self, emit, *, max_concurrent_sources=0):
        super().__init__(emit, max_concurrent_sources)
        self.handlers = {"weather": self._weather, "time": self._time}

    async def respond(self, event):
        payload = event["payload"]
        if payload.get("command") != "tool_run":
            return VOID
        handler = self.handlers.get(payload.get("name"))
        if handler is None:
            return self._result(event, ok=False,
                                error=f"未知工具: {payload.get('name')!r}")
        try:
            content = handler(payload.get("arguments") or {})
        except Exception as exc:
            return self._result(event, ok=False, error=str(exc))
        return self._result(event, ok=True, content=content)

    def _result(self, event, *, ok, content=None, error=None):
        return {
            "target": event["source"], "kind": "application",
            "payload": {"command": "tool_result", "ok": ok, "content": content,
                        "error": error, "task": event["payload"].get("task")},
        }

    def _weather(self, args):
        """演示级确定性伪天气：按城市名稳定生成。"""
        city = str(args.get("city") or "").strip()
        if not city:
            return "需要城市名"
        seed = sum(city.encode("utf-8"))
        temp = 16 + seed % 22
        condition = ["晴", "多云", "小雨", "阴"][seed % 4]
        return f"{city}：{condition}，{temp}°C"

    def _time(self, args):
        return time.strftime("%Y-%m-%d %H:%M:%S")
