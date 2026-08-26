"""Bash 设备：job 池 + 转后台 + 四级门控（参考实现，协议见 PROTOCOL.md）。

模型：
- respond 等 timeout 窗口：窗口内完成 → 同步返回 bash_result（短命令快
  路径）；超时 → 返回 bash_backgrounded 转后台，尾部由 job 任务驱动。
- 同源串行（SPEC 核心约束）：同一 source 的 job 排队——前一个终态后
  下一个才启动（排队中状态 queued，可 kill/status/extend）；跨 source
  并行。同源并行需求由请求方在 bash 语义内自行解决（nohup &/setsid）。
- 终态单发（单写者）：kill/deadline 只改状态并杀进程组，bash_result
  由 job 任务恰好发一次（queued job 无任务，由 kill/deadline 先到者发）。

四级门控：
- L1 timeout：转后台阈值（0 = 立即转后台）
- L2 deadline：请求发出起算，到期杀进程组（bash_result expired=true）
- L3 max_deadline：设备硬顶（构造校验），请求与 extend 均不可突破
- L4 设备终止：job 任务取消路径 killpg 连坐（start_new_session 独立
  进程组，无孤儿；终止不补发 result，属进程级故障）

归属：job 绑定创建者 source，跨 source 的 status/kill/extend 拒绝。
输出：缓冲 cap（超限丢头部，offset 越界 reset）；bash_result 给缓冲尾部
预览 + truncated，完整输出可 status 续读（截断不丢）。
"""

import asyncio
import os
import signal
import time
import traceback
from collections import deque

from my_team.kernel.process import VOID, UserModeProcess

RESULT_PREVIEW = 64 * 1024  # bash_result 内容预览上限（缓冲尾部）
STATUS_CHUNK = 64 * 1024    # bash_status_result 单次续读上限


class Job:
    """单条命令的生命周期记录（进程组 + 输出缓冲 + 终态标志）。

    state: queued（同源排队，未启动）| running | done | killed | expired。
    终态（done/killed/expired）先到者生效；queued 无 subprocess（proc=None）。
    """

    def __init__(self, job_id, source, tool_call_id, cmd, cwd, created_at,
                 deadline_at, timeout):
        self.job_id = job_id
        self.source = source            # 创建者（归属校验）
        self.tool_call_id = tool_call_id
        self.cmd = cmd
        self.cwd = cwd
        self.created_at = created_at
        self.deadline_at = deadline_at
        self.timeout = timeout
        self.proc = None                # subprocess（启动后赋值）
        self.buffer = b""               # 输出缓冲（超 cap 丢头部）
        self.trimmed = 0                # 缓冲已丢弃字节数（头部裁剪，截断判定用）
        self.state = "running"          # queued | running | done | killed | expired


class BashDevice(UserModeProcess):
    def __init__(self, emit, *, max_concurrent_sources, max_jobs,
                 timeout, deadline, max_deadline, output_cap, completed_cap):
        super().__init__(emit, max_concurrent_sources)
        if not (timeout < deadline <= max_deadline):
            raise ValueError(
                f"构造不变量失败: 需 timeout < deadline ≤ max_deadline，"
                f"got {timeout} < {deadline} ≤ {max_deadline}")
        self.max_jobs = max_jobs
        self.default_timeout = timeout
        self.default_deadline = deadline
        self.max_deadline = max_deadline
        self.output_cap = output_cap
        self.completed: deque[Job] = deque(maxlen=completed_cap)  # 完成保留（可回头查询）
        self.jobs: dict[str, Job] = {}    # 运行中
        self._pending: dict[str, deque[Job]] = {}  # 同源排队（queued）
        self._source_busy: dict[str, bool] = {}   # 同源串行：source → 有 job 未清空
        self._seq = 0

    # ------------------------------------------------------------------
    # 请求分发
    # ------------------------------------------------------------------

    async def respond(self, event):
        command = event["payload"].get("command")
        handlers = {"bash_run": self._on_run, "bash_status": self._on_status,
                    "bash_kill": self._on_kill, "bash_extend": self._on_extend}
        handler = handlers.get(command)
        if handler is None:
            return self._error(event, f"未知命令: {command!r}")
        return await handler(event)

    async def _on_run(self, event):
        """bash_run：校验 → 同源排队或启动。无排队时 respond 等 timeout
        窗口（窗口内完成返回 result，超时返回 backgrounded）。"""
        payload = event["payload"]
        if len(self.jobs) >= self.max_jobs:
            return self._error(event, f"运行中 job 达上限 {self.max_jobs}")
        cmd = payload.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            return self._error(event, "缺 cmd")
        timeout = payload.get("timeout", self.default_timeout)
        deadline = payload.get("deadline", self.default_deadline)
        if not isinstance(timeout, (int, float)) or not isinstance(deadline, (int, float)):
            return self._error(event, "timeout/deadline 必须为数值")
        if deadline > self.max_deadline:
            return self._error(event, f"deadline 超过设备上限 {self.max_deadline}")
        if not 0 <= timeout <= deadline:
            return self._error(event, "需满足 0 ≤ timeout ≤ deadline")
        now = time.monotonic()
        source = event["source"]
        job = Job(f"j{self._seq}", source, payload.get("tool_call_id"), cmd,
                  payload.get("cwd"), created_at=now,
                  deadline_at=now + deadline, timeout=timeout)
        self._seq += 1
        asyncio.create_task(self._deadline_watch(job))
        if self._source_busy.get(source):
            job.state = "queued"  # 同源串行：排队等前一个终态
            self._pending.setdefault(source, deque()).append(job)
            return VOID
        self.jobs[job.job_id] = job
        self._source_busy[source] = True
        window = asyncio.get_running_loop().create_future()
        asyncio.create_task(self._drive(job, window))
        return await window  # 同步窗口应答：result 或 backgrounded

    # ------------------------------------------------------------------
    # job 生命周期（单写者：终态恰发一次 bash_result）
    # ------------------------------------------------------------------

    async def _drive(self, job, window=None):
        """job 驱动：启动（queued 转 running）→ timeout 窗口判定 →
        转后台继续 → 终态单发 → 唤醒同源排队。

        window 非 None：窗口应答经 future 回给 respond（同步快路径）；
        否则直接 emit（排队 job / 转后台尾部）。
        """
        if job.proc is None:
            try:
                job.proc = await asyncio.create_subprocess_shell(
                    job.cmd, cwd=job.cwd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True)
            except OSError as exc:
                self._finish(job)
                self._deliver(job, self._result(job, error=str(exc)), window)
                self._wake_source(job.source)
                return
        try:
            if job.timeout > 0:
                try:
                    await asyncio.wait_for(self._drain(job), timeout=job.timeout)
                    self._finish(job)
                    self._deliver(job, self._result(job), window)
                    self._wake_source(job.source)
                    return
                except asyncio.TimeoutError:
                    self._deliver(job, self._backgrounded(job), window)
                    await self._drain(job)
            else:
                self._deliver(job, self._backgrounded(job), window)
                await self._drain(job)
        except asyncio.CancelledError:
            raise  # 设备终止：finally 连坐清理后传播，不补发 result
        except Exception as exc:
            # 意外异常：响亮（traceback + 回告 error）并清理槽位，
            # 防 _source_busy/jobs 泄漏导致该 source 永久挂起
            traceback.print_exc()
            self._finish(job)
            self.emit(self._result(job, error=str(exc)))
            self._wake_source(job.source)
            return
        finally:
            if job.proc.returncode is None:
                try:
                    os.killpg(job.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        await job.proc.wait()
        self._finish(job)
        self.emit(self._result(job))
        self._wake_source(job.source)

    async def _drain(self, job):
        """读输出到缓冲直至 EOF（EOF 即进程退出）。"""
        while True:
            chunk = await job.proc.stdout.read(65536)
            if not chunk:
                break
            self._append(job, chunk)
        if job.state == "running":
            job.state = "done"  # EOF 先行标记：防 deadline watch 误判自然完成
        await job.proc.wait()

    async def _deadline_watch(self, job):
        """到期兜底（L2）：仅当 job 仍运行且进程存活时设 expired 并杀进程组；
        queued 到期则从队列移除并直接发 result（queued 无驱动任务）。"""
        while True:
            wait = job.deadline_at - time.monotonic()
            if wait <= 0:
                # 守卫 proc 存在：queued→running 交接窗口（_drive 尚未建进程）时
                # 不判定——等待下轮重算，避免 AttributeError 致 deadline 静默失效
                if job.state == "running" and job.proc is not None \
                        and job.proc.returncode is None:
                    job.state = "expired"
                    try:
                        os.killpg(job.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif job.state == "queued":
                    self._pending[job.source].remove(job)
                    job.state = "expired"
                    self.emit(self._result(job))
                return
            await asyncio.sleep(wait)

    def _finish(self, job):
        self.jobs.pop(job.job_id, None)
        self.completed.append(job)

    def _wake_source(self, source):
        """同源串行推进：前一个终态后启动下一个排队 job；队列清空则释放。"""
        pending = self._pending.get(source)
        if not pending:
            self._source_busy.pop(source, None)
            return
        job = pending.popleft()
        job.state = "running"
        self.jobs[job.job_id] = job
        asyncio.create_task(self._drive(job))

    def _deliver(self, job, event, window):
        """窗口应答与总线 emit 二选一（respond 同步快路径 vs 任务自 emit）。"""
        if window is not None and not window.done():
            window.set_result(event)
        else:
            self.emit(event)

    def _append(self, job, chunk):
        job.buffer += chunk
        if len(job.buffer) > self.output_cap:
            job.trimmed += len(job.buffer) - self.output_cap
            job.buffer = job.buffer[-self.output_cap:]

    # ------------------------------------------------------------------
    # 控制面：status（offset 续读）/ kill / extend
    # ------------------------------------------------------------------

    async def _on_kill(self, event):
        job = self._find_owned(event)
        if job is None:
            return self._error(event, "未知 job 或无权访问")
        if job.state != "running":
            return self._error(event, f"job 已终态: {job.state}")
        job.state = "killed"
        try:
            os.killpg(job.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return VOID  # 终态回执 = 后续 bash_result(killed=true)

    async def _on_extend(self, event):
        job = self._find_owned(event)
        if job is None:
            return self._error(event, "未知 job 或无权访问")
        if job.state != "running":
            return self._error(event, f"job 已终态: {job.state}")
        extra = event["payload"].get("seconds")
        if not isinstance(extra, (int, float)) or extra <= 0:
            return self._error(event, "seconds 必须为正数")
        if job.deadline_at + extra - job.created_at > self.max_deadline:
            return self._error(event, f"延长后超过设备上限 {self.max_deadline}")
        job.deadline_at += extra
        return VOID

    async def _on_status(self, event):
        job = self._find_owned(event)
        if job is None:
            return self._error(event, "未知 job 或无权访问")
        offset = event["payload"].get("offset", 0)
        # offset 为逻辑位置（自输出开头）；缓冲裁剪对 agent 透明——
        # 小于已丢头部或超过总长视为越界 → reset（响应 offset 为当前可读起点）
        total = job.trimmed + len(job.buffer)
        if not isinstance(offset, int) or offset < job.trimmed or offset > total:
            return self._status_result(job, output=b"", offset=job.trimmed,
                                       reset=True, truncated=False)
        chunk = job.buffer[offset - job.trimmed:offset - job.trimmed + STATUS_CHUNK]
        new_offset = offset + len(chunk)
        return self._status_result(job, output=chunk, offset=new_offset,
                                   reset=False, truncated=new_offset < total)

    def _find_owned(self, event):
        """按 id 查 job：运行中或完成保留；排队 job 无 id 暴露，不可达。"""
        job_id = event["payload"].get("job_id")
        job = self.jobs.get(job_id)
        if job is None:
            for done in self.completed:
                if done.job_id == job_id:
                    job = done
                    break
        if job is None or job.source != event["source"]:
            return None
        return job

    # ------------------------------------------------------------------
    # 事件构造
    # ------------------------------------------------------------------

    def _result(self, job, error=None):
        ok = (job.state == "done" and job.proc is not None
              and job.proc.returncode == 0)
        return {"target": job.source, "kind": "application",
                "payload": {"command": "bash_result", "ok": ok,
                            "job_id": job.job_id,
                            "content": job.buffer[-RESULT_PREVIEW:].decode(
                                "utf-8", "replace"),
                            "exit_code": job.proc.returncode if job.proc else None,
                            "expired": job.state == "expired",
                            "killed": job.state == "killed",
                            "truncated": (job.trimmed + len(job.buffer))
                                          > RESULT_PREVIEW,
                            "duration": time.monotonic() - job.created_at,
                            "error": error,
                            "tool_call_id": job.tool_call_id}}

    def _backgrounded(self, job):
        return {"target": job.source, "kind": "application",
                "payload": {"command": "bash_backgrounded",
                            "job_id": job.job_id,
                            "tool_call_id": job.tool_call_id}}

    def _status_result(self, job, *, output, offset, reset, truncated):
        return {"target": job.source, "kind": "application",
                "payload": {"command": "bash_status_result",
                            "job_id": job.job_id, "state": job.state,
                            "output": output.decode("utf-8", "replace"),
                            "offset": offset, "reset": reset,
                            "truncated": truncated,
                            "duration": time.monotonic() - job.created_at,
                            "deadline_left": max(0.0, job.deadline_at
                                                 - time.monotonic()),
                            "tool_call_id": job.tool_call_id}}

    def _error(self, event, message):
        return {"target": event["source"], "kind": "application",
                "payload": {"command": "bash_error",
                            "job_id": event["payload"].get("job_id"),
                            "request": event["payload"].get("command"),
                            "error": message,
                            "tool_call_id": event["payload"].get("tool_call_id")}}
