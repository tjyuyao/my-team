"""Bash 设备：承接命令执行请求（最小实现：前台执行）。

对应 device/bash/PROTOCOL.md 草稿的最小子集：bash_run → bash_result。
后台/超时转后台/提醒等草稿特性未实现。
"""

import asyncio

from my_team.kernel.process import Process

MAX_OUTPUT_BYTES = 64 * 1024


class BashDevice(Process):
    def __init__(self, emit, *, max_concurrent_sources, cwd=None, timeout=30):
        super().__init__(emit, max_concurrent_sources)
        self.cwd = cwd
        self.timeout = timeout

    async def respond(self, event):
        payload = event["payload"]
        if payload.get("command") != "bash_run":
            return self._result(event, ok=False,
                                content=f"unexpected command: {payload.get('command')!r}",
                                exit_code=None, timed_out=False)
        timeout = payload.get("timeout", self.timeout)
        process = await asyncio.create_subprocess_shell(
            payload.get("cmd", ""),
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return self._result(event, ok=False,
                                content=f"[timed out after {timeout}s]",
                                exit_code=None, timed_out=True)
        output = output_bytes.decode(errors="replace")
        if len(output) > MAX_OUTPUT_BYTES:
            output = output[-MAX_OUTPUT_BYTES:] + "\n[truncated]"
        return self._result(event, ok=process.returncode == 0, content=output,
                            exit_code=process.returncode, timed_out=False)

    def _result(self, event, *, ok, content="", exit_code=None, timed_out=False):
        return {
            "target": event["source"],
            "kind": "application",
            "payload": {
                "command": "bash_result",
                "ok": ok,
                "content": content,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "tool_call_id": event["payload"].get("tool_call_id"),
            },
        }
