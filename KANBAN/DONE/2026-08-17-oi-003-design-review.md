# OI-003: 设计审查报告 — 目标品味评价、问题清单与下一步方向

**Opened:** 2026-08-17（v0.8.0 完成后全量设计审查）
**Status:** CONVERTED — 已拆分为 TODO（v0.9 P0）
**Converted to TODO:** 2026-08-17-p0-write-path-security.md, 2026-08-17-p0-transactional-pending-ops.md, 2026-08-17-p0-tick-result-truth.md, 2026-08-17-dead-module-cleanup.md, 2026-08-17-consistency-cleanup.md
**Source:** 全量源码 + SPEC + KANBAN 里程碑（v0.1.0–v0.8.0）
**Priority:** high

---

## 一、目标品味评价（Taste）

### 值得肯定（好品味）

1. **内核与 Agent 行为协议分离**：`Tick` 是内核状态提交单位，`Activation`
   是 Agent 唤醒单位，`ReAct Turn` 是逻辑回合。这是整个项目最重要、
   也最正确的架构决定。`AgentContinuation` + `PendingOperationRegistry`
   让 Agent 可以跨 tick 恢复思考，避免"在一个 tick 内阻塞等 LLM"。

2. **两阶段 Validate + 效果分组原子性**：`PreValidate`（是否可以尝试）
   与 `CommitValidate`（现在是否仍可提交）区分了意图合法性检查和提交
   时前置条件检查，是数据库事务思想在 Agent 内核中的正确应用。
   `DelegateIntent` 的 `TASK_CREATE + EMAIL_SEND` 同组原子性是加分项。

3. **安全品味诚实**：
   - OI-001 明确拒绝在沙箱协议完成前开放 Bash，论证了 Bash 是
     "万能后门"，这个判断非常正确且少见。
   - `ExecutionClass` 区分 `LOCAL_PROCESS` 与 `SANDBOXED_PROCESS`，
     L0/L1 Python 执行如实声明"防意外，不防恶意逃逸"。
   - `OperationPolicy` 默认拒绝（deny-by-default）。
   - 这些都属于"知道自己不知道什么"的好品味。

4. **工程纪律**：SQLite 单事务保存全部组件、state epoch fencing、
   outbox 幂等投递、761 测试 + ruff/mypy clean + KANBAN 状态流转，
   过程记录完整。

### 需要警惕（品味风险）

1. **宽度优先、收敛不足**：每轮都新增机制（continuation、manifest、
   executor admission、L0/L1），但旧机制未退役或未接入主路径
   （TickEngine、IdentityEnforcer、Executors、FileOps 等，详见问题 5）。
   这会让仓库看起来比实际运行时更完整。

2. **测试覆盖有被当作安全证明的倾向**：路径安全测试覆盖的是
   `FileOps`/`PrivateStore`，但真实提交路径不走这些模块（见问题 1）。
   高覆盖率测的是"隔离组件行为正确"，不是"系统主路径安全"。

3. **文档/版本速度与代码一致性问题**：同一仓库内 `__version__=0.3.0`、
   `pyproject version=0.4.0`、里程碑到 v0.8.0；SPEC §8.2 与 §8.6
   描述了两套 10 阶段，而代码里还留着 7 阶段 TickEngine。文档繁荣
   但权威性被稀释。

**总体评价**：目标本身品味良好——用事务/调度/能力的语言来约束
多 Agent 系统，方向正确，且对"未完成的安全边界"有清醒认知。
主要问题不在目标，而在**主路径与隔离组件的脱节**以及**多套阶段
模型并存造成的观察者混乱**。

---

## 二、发现的问题

### P0-1（严重/安全）：FILE_WRITE 提交路径存在路径穿越，真实写路径绕过 PrivateStore/FileOps 防护

**位置**：`src/my_team/simulation.py::_phase_commit` FILE_WRITE 分支，
约 L3100-3120。

**现象**：`_phase_commit` 直接执行
`target = home / path; target.parent.mkdir(...); target.write_text(...)`，
未调用 `PrivateStore.resolve_path()` 或 `FileOps.write()`。
`PrivateStore` 的 `../` 穿越与 symlink 防护只在隔离测试中验证，
真实提交路径完全绕过。

**验证**（无需改代码即可复现）：

```python
sim._transaction_buffer.stage(
    effect_type=EffectType.FILE_WRITE,
    agent_id="agent.a",
    resource="../agent.b/workspace/pwned.txt",
    data={"content": "pwned"},
)
sim._phase_commit(0, {})
# → private/agent.b/workspace/pwned.txt 被创建
```

**影响**：任何 Agent（或 LLM 生成的写意图）只要提交
`path="../<其他 agent>/..."` 或绝对路径，就能写其他 Agent 私人空间、
甚至宿主机路径。这违反了 SPEC §5.2 / §15.1 的核心隔离假设。

**建议方向**：
- Commit apply 统一走 `PrivateStore.resolve_path(agent_id, path)`，
  或直接复用 `FileOps.write` 的校验逻辑。
- `WritePrivateFileIntent` 在 PreValidate 增加路径静态检查
  （拒绝绝对路径与 `..` 段）。
- 安全测试必须覆盖 Simulation 主路径（`write` 工具 → commit），
  而不只是 FileOps 单元测试。

### P0-2（严重/正确性）：Pending operation 注册不参与事务，ROLLBACK 后产生孤儿 op 与错误 continuation

**位置**：`src/my_team/simulation.py::_phase_act`（`self._pending_ops.submit`
直接注册，L2180-2200 附近）与 `_phase_commit`（回滚时未撤销
`_pending_ops` 与 `AgentContinuation`/`AgentState`）。

**现象**：Act 阶段直接向 `PendingOperationRegistry` 注册 LLM/tool op，
并推进 continuation 到 `WAITING_FOR_LLM/TOOL`；Commit 阶段若 rollback，
回滚逻辑只恢复文件/KB/任务/outbox，**不撤销**本 tick 注册的 pending op，
也不恢复 continuation/agent state。

**验证**：构造一个同时产生 `SubmitLLMRequest` 和非法
`WritePrivateFileIntent`（例如 path="workspace" 触发 IsADirectoryError）
的 Agent，连续 run_tick：

```text
tick 0: rolled_back=True, pending_ops=[op1 SUBMITTED], phase=WAITING_FOR_LLM, epoch=1
tick 1: rolled_back=True, pending_ops=[op1, op2],       phase=WAITING_FOR_LLM, epoch=2
tick 2: rolled_back=True, pending_ops=[op1, op2, op3],  phase=WAITING_FOR_LLM, epoch=3
```

孤儿 SUBMITTED op 永不消费、继续占用请求预算；Agent 卡在
WAITING_FOR_LLM，下一 tick 又被唤醒重复提交。

**影响**：回滚语义失效；资源泄漏；成本（LLM 请求）可能在未提交的
tick 后继续发生；重放去重历史（seen_requests）也被污染。

**建议方向**：
- 将 pending op 注册纳入 tick 事务边界：Act 只 stage 一个
  `PENDING_OP_REGISTER` effect，Commit 成功后才真正注册；或维护
  "本 tick 注册清单"，rollback 时撤销 op + 恢复 continuation +
  恢复 agent state machine。
- 回归测试：`rollback + SubmitLLMRequest/SubmitToolRequest 共存`
  必须断言 tick 结束后无新增 pending op、continuation 回到
  `READY_TO_DECIDE/FRESH`、seen_requests 不残留。

### P0-3（严重/可观察性）：`run_tick()` 返回的 TickResult 来自已退役的 7 阶段引擎，不反映真实内核

**位置**：`src/my_team/simulation.py::run_tick` 末尾
`results = self._tick_engine.advance(1); return results[0]`；
`src/my_team/tick_engine.py` 仍实现 7 阶段 stub。

**现象**：真实内核跑 10 阶段（`last_tick_phases` 已证明），但公开
返回值 TickResult 的 `phases_completed` 是 legacy TickEngine 的
`freeze/deliver/observe/decide/act/commit/audit`；rollback 时
`TickResult.committed=True`、`errors=[]`，与 `_last_tick_rolled_back=True`
矛盾。

**验证**：

```python
res = sim.run_tick()  # tick 实际 rollback
res.committed       # True（应为 False 或至少反映内核结果）
res.phases_completed  # 7 个 legacy 阶段（真实是 10 阶段）
```

**影响**：调用方、监控、回放入口拿到错误的内核协议信息。

**建议方向**：
- `Simulation.run_tick` 直接构造 `TickResult`，以
  `self._last_tick_phases` 和 `_last_tick_rolled_back` 为准。
- 将 TickEngine 降级为纯时钟（`current_tick/state/advance` 只管理
  时钟），删除其 7 阶段 stub 或让 Simulation 不再调用 `advance(1)`。

### P1-4（架构）：TickEngine 成为"僵尸"——两套阶段机并存

**位置**：`src/my_team/tick_engine.py`。

**现象**：`TickEngine` 保留完整的 7 阶段循环、默认 phase handler、
`TickSnapshot`/`TickResult`，但 `Simulation.run_tick` 自己实现真实的
10 阶段内核，每 tick 又调用 `tick_engine.advance(1)` 跑一遍无意义的
7 阶段循环。两个 `TickResult`、两套 snapshot、两套 phase 语义并存。

**建议方向**：
- 方案 A：Simulation 完全拥有内核，TickEngine 仅保留时钟与状态。
- 方案 B：把 Simulation 的 10 阶段上移到 TickEngine，Simulation 只做
  handler 注册。
- 无论选哪种，仓库内只保留一套阶段定义，并同步 SPEC §8.2/§8.6。

### P1-5（架构）：安全/执行模式模块"测试中存活、主路径未接线"

**位置**：
- `src/my_team/identity.py::IdentityEnforcer` — 只在
  `tests/test_identity_security.py` 中测试，Simulation 从未实例化；
  `validate_file_access` 为空实现（`pass`）。
- `src/my_team/executors.py` — `DiscreteAsyncExecutor` /
  `BoundedMicroLoopExecutor` 只被 `tests/test_executors.py` 测试，
  Simulation 主路径未使用。
- `src/my_team/file_ops.py` — Simulation 构造 `self._file_ops` 但从不
  调用；真实读写在 handler/commit 内联实现（且写路径未校验）。
- `src/my_team/delegation.py::DelegationProtocol` — 只通过 property
  暴露，委托逻辑实际内联在 `_phase_act`。

**影响**：给读者和审查者造成"存在安全层/执行模式层"的印象，
但真实系统没有经过这些层；隔离测试无法保护主路径（P0-1 就是
证据）。

**建议方向**：
- 明确每个模块的定位：主路径必用 / 外部 API / 废弃待删。
- 优先把 `FileOps` 与 `PrivateStore.resolve_path` 接入 commit 写路径；
  `IdentityEnforcer` 要么接入 ToolContext 创建，要么删除；`executors.py`
  若 v0.9+ 才启用，在模块 docstring 显式标注"not wired yet"。

### P1-6（文档/一致）：多套阶段模型与事件可见性语义不一致

**位置**：
- SPEC §8.2：10 阶段以 Freeze 开头（Freeze → Deliver → Schedule →
  Observe → Decide → Validate → Act → Commit → Publish → Audit）。
- SPEC §8.6：10 阶段以 Ingest 开头（Ingest → Freeze → Schedule →
  Observe → Decide → Validate → Act → Commit → Publish → Audit）。
- 代码实际：Ingest → Deliver → Freeze → Schedule → …，但
  `_last_tick_phases` 记录为 `ingest, freeze, schedule, observe,
  decide, validate, act, commit, publish, audit`（deliver 消失）。
- `WakeupEvent` docstring 与 scheduler 注释声称事件在 tick t+1 可见，
  但 `compute_ready_set` 只检查 `event.tick <= tick`；同一 tick 内
  Ingest/Deliver 刚入队的事件会在同 tick 的 Schedule 被消费。

**建议方向**：选定一个 phase 模型（建议以 SPEC §8.6 的 Ingest 开头
10 阶段为准）全仓库同步；给 WakeupEvent 增加显式
`visible_at_tick` 字段，`_matches` 用它而非排序副作用。

### P1-7（工程）：版本元数据陈旧

**位置**：`src/my_team/__init__.py`（`__version__ = "0.3.0"`）、
`pyproject.toml`（`version = "0.4.0"`）、里程碑已到 v0.8.0。

**建议方向**：统一为单一权威版本号（建议从 pyproject 读取），
并在 CI 中加一个"版本一致性"检查。

### P2-8（一致性）：dispatch 使用最新快照，而非 ToolRequest 的 workspace_version

**位置**：`src/my_team/simulation.py::_phase_dispatch` TRUSTED_IN_PROCESS
分支，`read_view=(self._last_snapshot or {})...`。

**现象**：op 提交时记录了 `workspace_version`（Freeze 视图哈希），
但跨 tick 排队后 dispatch 时读取的是**下一个 tick** 的 `_last_snapshot`。
排队的工具可能读到比决策时更新的文件内容，违背读一致性。
（apply-time base-hash 只保护回写，不保护 transform 的输入读取。）

**建议方向**：为 SUBMITTED op 保存提交时的 freeze 视图（或至少
`workspace_version` + 内容哈希），dispatch 校验并优先使用提交视图；
否则对陈旧视图声明失败。

### P2-9：run_tests 与 git 工具直接使用宿主项目根目录

**位置**：`handle_run_tests` / `handle_git_diff` / `handle_git_status`
均 `cwd=str(Path.cwd())`。

**现象**：`run_tests` 运行 `uv run pytest -q`，能读取宿主项目全部
源文件、`.venv`、`private/` 等；这已在 manifest 中诚实披露为
`LOCAL_PROCESS`（OI-001 范畴），但设计上应明确其非目标边界，
并推进 v0.8.0 P2-7 的 SANDBOXED_PROCESS 版本。

---

## 三、建议的下一步方向

1. **先收口正确性与安全（P0）**：
   - 修复 FILE_WRITE 路径穿越，并把路径安全测试搬到 Simulation 主路径。
   - 把 pending op 注册纳入事务边界，补 rollback 回归测试。
   - 让 `run_tick()` 返回真实的 10 阶段 TickResult（含 committed/
     errors 真相）。

2. **再做结构收敛（P1）**：
   - 退役或合并 TickEngine，统一 SPEC 与代码的阶段模型。
   - 清理/接线 `FileOps`、`IdentityEnforcer`、`Executors`、
     `DelegationProtocol`，消除"测试中存活"的假象。
   - 统一版本号，CI 增加一致性检查。

3. **继续 v0.8.0 遗留 P2**：
   - P2-7：run_tests 真实隔离（SANDBOXED_PROCESS 才有的只读挂载、
     网络 deny-by-default、资源限制）。
   - P2-8：Snapshot 覆盖矩阵（把 P0-2 这类"回滚未覆盖 pending ops"
     的问题系统化地测出来）。
   - P2-11：token/cost 预算（与 P0-2 修复相关：未提交 tick 产生的
     LLM 成本必须可审计、可拒绝）。

4. **主路径安全测试原则**：凡是安全/一致性相关测试，至少有一条
   必须穿过 `Simulation.run_tick()` 或 `_phase_commit()` 主路径，
   禁止只测隔离模块。

---

## 四、当前验证数据（2026-08-17 复测）

```text
uv run pytest -q                        # 761 passed
uv run ruff check src tests             # All checks passed
uv run mypy src/my_team                 # Success, 40 source files
pytest --cov 行覆盖                       # 93.56%（4345/4644）
pytest --cov 分支覆盖                     # 87.50%（1099/1256）
```

> 覆盖率高，但 P0-1/P0-2 均未被主路径测试捕获，说明覆盖结构存在
> "隔离模块覆盖多、主路径集成覆盖少"的盲区。

---

## 产出

- 本报告本身为 OPEN_ISSUE 记录。
- 待确认后，将 P0-1、P0-2、P0-3 拆分为独立 TODO 任务卡片。

## 验收标准（关闭本 OPEN_ISSUE 的条件）

- [ ] P0-1：`write` 路径穿越在 Simulation 主路径测试中被拒绝
- [ ] P0-2：rollback 后本 tick 注册的 pending op 为 0，continuation/
      agent state 恢复，测试通过
- [ ] P0-3：`run_tick()` 返回的 TickResult 与 `last_tick_phases` /
      `_last_tick_rolled_back` 一致
- [ ] P1-4：仓库内仅保留一套阶段引擎（或明确 TickEngine 为时钟）
- [ ] P1-5：FileOps/IdentityEnforcer/Executors 等模块有明确接线状态
- [ ] P1-6：SPEC 阶段模型与代码一致，事件可见性有显式字段
- [ ] P1-7：版本号统一
