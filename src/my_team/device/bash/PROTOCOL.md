# Bash 设备协议

Bash 设备是设备进程：承接命令执行请求，以 job 池管理前台/后台任务。
实现见 `device.py`（参考实现，可复制进工作目录经 install_device 装载）。

## 设计要点

- **同步窗口 + 转后台**：respond 等 timeout 阈值——窗口内完成 → 同步返回
  `bash_result`（短命令快路径）；超时 → 返回 `bash_backgrounded` 转后台
  继续运行，尾部由设备任务驱动。timeout=0 表示立即转后台。timeout 只
  作用于前台判定窗口，转后台后不再受其约束。
- **同源串行（SPEC 核心约束）**：同一请求方的 job 排队——前一个终态后
  下一个才启动；跨请求方并行。同源并行不是设备功能，由请求方在 bash
  语义内自行解决（`nohup &`/`setsid`）。排队中 job **无 job_id 暴露**
  （请求方不可控制）；启动时 emit bash_backgrounded 携带 job_id，
  此后可 status/kill/extend。
- **终态单发**：bash_result 恰好发一次，以先到达的终止条件为准
  （kill / deadline / 自然退出三选一）。请求方须容忍乱序：可能先收
  status_result(running) 后收 bash_result。
- **四级门控**：timeout（转后台阈值）→ deadline（兜底，到期杀进程组）→
  max_deadline（设备硬顶，请求与 extend 均不可突破）→ 设备终止（连坐
  全部 job 进程组）。构造不变量：timeout < deadline ≤ max_deadline。
- **归属**：job 绑定创建者 source；跨 source 的 status/kill/extend 一律拒绝。
- **截断不丢**：bash_result 内容为缓冲尾部预览（64KB）+ truncated 标记；
  完整输出保留在缓冲，可经 bash_status 按 offset 续读。缓冲超上限丢头部，
  旧 offset 失效时 status 返回 reset=true。
- **无孤儿**：每个 job 独立进程组（start_new_session）；设备被终止时连坐
  杀全部 job 进程组（详见下方"沙箱语义"节）。

## 事件

事件为 application 层，payload.command 路由；source 由宿主注入，target 由发送方填写。
所有请求事件可带 `tool_call_id`（回显于对应结果事件）；所有拒绝回告经
`bash_error`（唯一请求级失败载体）。

### bash_run（请求方 → 设备）

```json
{
  "source": "a", "target": "bash", "kind": "application",
  "payload": {"command": "bash_run", "cmd": "pytest -q", "cwd": "/work",
              "timeout": 30, "deadline": 600, "tool_call_id": "t1"}
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| cmd | str | 要执行的命令（必填） |
| cwd | str | 工作目录，可选；缺省 = 数据家（MY_TEAM_DATA_DIR，绑定 agent 的家） |
| timeout | float | 转后台阈值（秒）；缺省用设备默认；0 = 立即转后台 |
| deadline | float | 兜底长超时（秒，请求发出起算）；缺省用设备默认；须 ≤ max_deadline |

校验：`0 ≤ timeout ≤ deadline ≤ max_deadline`，且运行中 job 数 < max_jobs；
非法即 bash_error（拒绝，不钳制）。

**bash_run 应答矩阵**（请求方据此决定是否等待/何时干预）：

| 应答 | 条件 | 后续 |
| --- | --- | --- |
| bash_result | timeout 窗口内完成 | 无（终态已达） |
| bash_backgrounded（含 job_id） | 超时转后台 / timeout=0 | 尾部 result 事件；此时起可 status/kill/extend |
| VOID | 同源排队受理（无 job_id） | 启动时 emit backgrounded（含 job_id），此后可控 |

### bash_error（设备 → 请求方）

唯一请求级失败载体（缺 cmd、超限、越权、未知 job/命令、参数非法等）。

```json
{"payload": {"command": "bash_error", "job_id": "j0", "request": "bash_run",
             "error": "deadline 超过设备上限 600", "tool_call_id": "t1"}}
```

request 为出错的原请求命令名。

### bash_backgrounded（设备 → 请求方）

命令转后台时产出（timeout 触发或 timeout=0 立即）。无输出内容——输出经
bash_status 续读。

```json
{"payload": {"command": "bash_backgrounded", "job_id": "j0", "tool_call_id": "t1"}}
```

### bash_result（设备 → 请求方）

job 终态统一结果事件（自然完成 / 被杀 / 到期）。终态优先级：killed 与
expired 先到者生效，此时 ok 恒 false；exit_code≠0 的自然完成 ok=false 且
expired/killed=false。

```json
{"payload": {"command": "bash_result", "ok": true, "job_id": "j0",
             "content": "1 passed", "exit_code": 0, "expired": false,
             "killed": false, "truncated": false, "duration": 1.2,
             "tool_call_id": "t1"}}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| ok | bool | 是否正常结束（exit_code==0 且未被杀/到期） |
| content | str | 缓冲尾部预览（≤64KB，UTF-8，替换非法字节） |
| exit_code | int \| null | 退出码 |
| expired / killed | bool | 是否到期被杀 / agent 主动杀死 |
| truncated | bool | content 是否被截断（完整输出可 status 续读） |
| error | str \| null | 启动失败等原因（ok=false 时） |
| duration | float | 自请求发出起算时长（秒，含排队等待） |

### bash_status（请求方 → 设备）

```json
{"payload": {"command": "bash_status", "job_id": "j0", "offset": 2048,
             "tool_call_id": "t1"}}
```

offset：上次读取结束位置（**逻辑位置**，自输出开头计；缓冲裁剪对请求方
透明）；越界（小于已丢头部或超过总长，含缓冲裁剪后失效）返回 reset=true，
并以响应中的 offset 为当前可读起点续读。

### bash_status_result（设备 → 请求方）

```json
{"payload": {"command": "bash_status_result", "job_id": "j0", "state": "running",
             "output": "[增量]", "offset": 3072, "reset": false,
             "truncated": false, "duration": 2.1, "deadline_left": 597.9,
             "tool_call_id": "t1"}}
```

state：`running` / `done` / `killed` / `expired`（queued 无 id 暴露，不可
查询）。output 为自 offset 起的增量（≤64KB，truncated 标记未完）；下次
续读传回新 offset。duration 与 deadline_left 均为自请求发出的剩余语义
（秒）。

### bash_kill（请求方 → 设备）

```json
{"payload": {"command": "bash_kill", "job_id": "j0", "tool_call_id": "t1"}}
```

杀进程组；成功即 VOID（无同步应答），终态回执 = 后续 bash_result(killed=true)。

### bash_extend（请求方 → 设备）

```json
{"payload": {"command": "bash_extend", "job_id": "j0", "seconds": 120,
             "tool_call_id": "t1"}}
```

延长 deadline（仍受 max_deadline 硬顶约束）；成功即 VOID。

## 设备配置（构造参数，全部必填无默认）

经工作目录装载时由 install_device 的 options 传入；构造时校验不变量
`timeout < deadline ≤ max_deadline`，违反即装载失败（响亮）。

| 参数 | 说明 |
| --- | --- |
| max_concurrent_sources | 跨 source 并发的 respond 上限（0 = 无限；内核进程级） |
| max_jobs | 同时运行的 job 数上限（跨 source 共享；排队 job 不计入，超限拒绝新请求） |
| timeout | 默认转后台阈值（秒） |
| deadline | 默认兜底长超时（秒） |
| max_deadline | 请求与 extend 均不可突破的硬顶（秒） |
| output_cap | 每 job 输出缓冲上限（字节，超限丢头部） |
| completed_cap | 完成保留的 job 条数上限（可回头 status 查询） |

## 边界（第一版）

- **同源串行代价**：timeout 窗口内，同一请求方的后续请求（含控制命令）
  排队等该 respond 返回（FIFO 保序）——要即时控制用 timeout=0 或小
  timeout。
- **交互命令/TTY**：stdin 为 DEVNULL，不支持 less/vim 等交互命令（文档声明）。
- **设备重启/重装**：job 表随进程丢失，语义为"旧 job 不可恢复"，未知
  job 一律 bash_error。
- **卸载/终止**：设备被终止 = 连坐杀全部 job 进程组（不补发 result）；
  排队中的未处理事件随进程消亡（无声消失）。
- **输出内存**：每 job 缓冲 ≤ output_cap，完成保留 ≤ completed_cap 条——
  大输出受此约束，内存可控。

## 沙箱语义

bash 设备是普通设备身份，经 bwrap 固定矩阵沙箱
启动（`kernel/process.py` `_sandbox_reexec`）。所有 bash 命令作为该进程的
子进程在同一沙箱内执行（`asyncio.create_subprocess_shell`，继承沙箱上下文）。

**固定矩阵语义**（对所有进程一致，无 per-position 物化）：

- **系统只读**：`--ro-bind / /`，根文件系统只读挂载；
- **`/dev` = devtmpfs**：`--dev /dev`（置于 `--ro-bind / /` 之后覆盖 /dev
  挂载点）构造全新 devtmpfs，`/dev/null` 等字符设备可写——bash 命令中
  `>/dev/null`、`open('/dev/null', 'r+')` 均正常工作；
- **`/tmp` 可写**：`--tmpfs /tmp`；
- **数据根掩蔽**：`--tmpfs <data_root>`，沙箱内仅自己的 private home 可见；
- **挂载锚点**：设备 home `data/<device-id>` 可写，代码与状态均在此；
- **默认 cwd = 数据家**：内核在 `_sandbox_reexec` 的 env 里注入
  `MY_TEAM_DATA_DIR = writable[0]`（绑定 agent 的家）；`BashDevice.__init__`
  读取后作为 `self.default_cwd`；`bash_run` 命令不指定 `cwd` 时以此为 cwd，
  指定则用指定值（沙箱矩阵约束其可达范围，系统路径只读，跨家不可见）。
  环境变量随 bash 子命令继承（设备进程内本就继承，可接受）；
- **默认禁网**：`--unshare-net`；设备经安装 payload `options.needs_network=true`
  声明后方保留网络面（进程级资源开关，非权限 scope）；
- **ns 隔离**：`--unshare-user`（永不 root）、`--unshare-pid`（pidns，不可见
  宿主/兄弟进程）、`--unshare-ipc`（封 System V IPC）；
- **`--die-with-parent`**：bwrap 宿主杀死时沙箱连坐。

**进程组连坐与沙箱强杀（互为冗余）**：

bash 设备对每个 job 独立创建进程组（`start_new_session=True`），终止 /
deadline 到期时以 `killpg(SIGKILL)` 连坐整个进程组（L4 门控）——这是
**单 job 资源控制手段**，防止 job 子进程成孤儿。

命令内部可调用 `setsid` 逃出当前进程组，但**逃逸不出沙箱**：新会话的进程
仍在 bwrap 的 pidns 内（隔离的 PID namespace，看不到宿主进程，亦无法信号
宿主）。设备进程终止 → bwrap（沙箱 PID 1）消亡 → 整个 pidns 内所有进程
强杀（内核行为，无需应用层干预）；加之 `--die-with-parent` 保证 bwrap
随宿主内核进程一同消亡。因此四级门控（killpg）与沙箱 pidns 强杀**互为
冗余**，不冲突：前者精细控制单 job，后者兜底整 ns。

## 待定项

- **watch（输出 marker 通知）**：输出出现指定字符串即通知——依赖输出流
  增量扫描机制，后续版本。
- **bash_list（job 盘点）**：job 归 source，agent 只关心自己的 job，暂缓。
- **remind（到期提醒）**：与 deadline 语义重叠，已从协议移除，不再考虑。
