# Bash 设备协议（草稿）

Bash 设备是设备进程，它承接命令执行请求并管理前台/后台任务。
本文件是方案草稿，尚未实现，待设计确认后落地。

## 设计要点

- 前台命令正常执行，完成时返回结果。
- 命令超过短超时（timeout）时**自动转后台**：不杀进程，转为后台 job 继续运行，
  并通知请求方；agent 不被阻塞，可继续干别的。
- 后台 job 由 agent 决定后续：查询状态，或主动杀死（kill）。
- 兜底长超时（deadline）：每次请求可由 agent 设置，到期强制杀，防止后台任务失控。
- 到期提醒（remind_before）：可由 agent 设置，到期前向请求方提醒。

## 事件

事件为 application 层，payload.command 路由；source 由宿主注入，target 由发送方填写。

### bash_run（请求方 → 设备）

```json
{
  "source": "9f8e7d6c5b4a",
  "target": "1a2b3c4d5e6f",
  "kind": "application",
  "payload": {
    "command": "bash_run",
    "cmd": "pytest -q",
    "cwd": "/work",
    "timeout": 30,
    "deadline": 600,
    "remind_before": 60
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| cmd | str | 要执行的命令 |
| cwd | str | 工作目录，可选 |
| timeout | float | 转后台阈值（秒）：超过则自动转后台，可选 |
| deadline | float | 兜底长超时（秒）：到期强制杀，可选 |
| remind_before | float | deadline 到期前提醒（秒），可选 |

### bash_result（设备 → 请求方）

前台完成、被杀、或 deadline 到期的统一结果事件。

```json
{
  "source": "1a2b3c4d5e6f",
  "target": "9f8e7d6c5b4a",
  "kind": "application",
  "payload": {
    "command": "bash_result",
    "ok": true,
    "job_id": "j1",
    "content": "1 passed",
    "exit_code": 0,
    "expired": false,
    "killed": false,
    "duration": 1.2
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | 命令是否正常结束 |
| job_id | str | 对应 job |
| content | str | 输出（截断规则同前） |
| exit_code | int | 退出码，可能为 null |
| expired | bool | 是否 deadline 到期被杀 |
| killed | bool | 是否 agent 主动杀死 |
| duration | float | 实际执行时长 |

### bash_backgrounded（设备 → 请求方）

命令超时自动转后台时产出。

```json
{
  "source": "1a2b3c4d5e6f",
  "target": "9f8e7d6c5b4a",
  "kind": "application",
  "payload": {
    "command": "bash_backgrounded",
    "job_id": "j1",
    "partial_output": "[Showing first 2000 bytes]",
    "reason": "timeout"
  }
}
```

### bash_status（请求方 → 设备）

```json
{
  "source": "9f8e7d6c5b4a",
  "target": "1a2b3c4d5e6f",
  "kind": "application",
  "payload": {"command": "bash_status", "job_id": "j1"}
}
```

### bash_status_result（设备 → 请求方）

```json
{
  "source": "1a2b3c4d5e6f",
  "target": "9f8e7d6c5b4a",
  "kind": "application",
  "payload": {
    "command": "bash_status_result",
    "job_id": "j1",
    "state": "running",
    "output": "[增量输出]"
  }
}
```

state：`running` / `done` / `killed` / `expired`。

### bash_kill（请求方 → 设备）

```json
{
  "source": "9f8e7d6c5b4a",
  "target": "1a2b3c4d5e6f",
  "kind": "application",
  "payload": {"command": "bash_kill", "job_id": "j1"}
}
```

设备杀进程组后产出 `bash_result`（killed=true）。

### bash_reminder（设备 → 请求方）

deadline 到期前提醒。

```json
{
  "source": "1a2b3c4d5e6f",
  "target": "9f8e7d6c5b4a",
  "kind": "application",
  "payload": {
    "command": "bash_reminder",
    "job_id": "j1",
    "seconds_left": 60,
    "deadline": 600
  }
}
```

## 行为（草稿，待定）

- 前台命令在 timeout 内结束 → `bash_result`（正常完成）。
- 超过 timeout 未结束 → 转后台，emit `bash_backgrounded`；命令继续运行，agent 不被阻塞。
- 后台 job 由 agent 决定后续：`bash_status` 查询、`bash_kill` 杀死。
- 设置了 deadline 的 job：到期强制杀进程组，emit `bash_result`（expired=true）。
- 设置了 remind_before 的 job：deadline 前提醒一次（或多次？待定）。

## 待定项

- 后台 job 的**输出获取**：增量查询（status 返回新输出）还是全量缓冲？job 输出可能很大，需截断策略。
- **提醒频率**：到期前提醒一次还是按间隔多次？
- **job 上限**：同时存在多少后台 job？超限策略（拒绝新命令还是转拒绝）？
- **设备进程模型**：内核已异步化（respond 可并发）——后台 job 的作业池
如何组织（跨 source 并行已由内核承担，同 source 后台 job 管理待定）？
- **沙箱**：bwrap + setrlimit + 进程组杀（见 LLM 设备同款原则），未实现。
- **透传 TTY/交互命令**：`less`、`vim` 等交互命令如何处理？待定（可能直接拒绝或文档声明不支持）。
