"""Bash 设备（最小实现）：前台执行命令并返回结果。

对应 device/bash/PROTOCOL.md 草稿的最小子集：bash_run → bash_result。
后台/超时转后台/提醒等草稿特性未实现。
"""

import subprocess

from my_team.kernel.process import Process

MAX_OUTPUT_BYTES = 64 * 1024


class BashDevice(Process):
    def __init__(self, emit, *, cwd=None, timeout=30):
        super().__init__(emit)
        self.cwd = cwd
        self.timeout = timeout

    def respond(self, event):
        payload = event["payload"]
        if payload.get("command") != "bash_run":
            return self._result(event, ok=False,
                                content=f"unexpected command: {payload.get('command')!r}",
                                exit_code=None, timed_out=False)
        try:
            proc = subprocess.run(
                payload.get("cmd", ""),
                shell=True,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=payload.get("timeout", self.timeout),
            )
            content = (proc.stdout or "") + (proc.stderr or "")
            if len(content) > MAX_OUTPUT_BYTES:
                content = content[-MAX_OUTPUT_BYTES:] + "\n[truncated]"
            return self._result(event, ok=proc.returncode == 0, content=content,
                                exit_code=proc.returncode, timed_out=False)
        except subprocess.TimeoutExpired:
            return self._result(event, ok=False,
                                content=f"[timed out after {payload.get('timeout', self.timeout)}s]",
                                exit_code=None, timed_out=True)

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
