# Simulation 模块地图（通俗解说 ↔ 代码逐项对应）

> 目的：把 "Simulation 到底做了什么" 用一套通俗心智模型讲清楚，并让每个
> 通俗环节都能在代码里找到精确落点。阅读对象：开发此项目的人类与 agent。
> 更新纪律：本文件与代码同步演进；方法名与阶段序列是锚，行号不写死。

## 一句话总览

**Simulation 是"模拟世界的操作系统 + 回合制裁判"**：管理组织树里的所有
Agent 在同一个世界里按回合（tick）运转、状态统一落定、失败整体回滚、
全程留账。所有并行/并发问题通过"每 tick 一轮 + 事务 + 锁"串行化消化，
不做任何全量状态快照（T17 已将冻结视图按需化）。

## 一、全景：Simulation 由哪些"部件群"组成

| 通俗角色 | Python 模块/类 | 一句话职责 |
|---|---|---|
| 总导演/比赛服务器 | `simulation.py` 的 `Simulation` | 唯一"大脑"：握所有部件、执行业务规则、驱动一回合；其余模块多是它的部件或数据模型 |
| 回合规则手册 | `tick_engine.py` 的 `TickEngine` | 定义"一回合"：tick 计数、周期配置、回合阶段清单 |
| 叫醒服务 | `scheduler.py` 的 `AgentScheduler` | 决定每回合谁该起床（事件/邮件/任务到期/闹钟）；每 Agent 每回合最多 1 次激活 |
| 视角生成器 | `context_compiler.py` 的 `ContextCompiler` | 把世界状态编译成每个 Agent 的"眼前所见"（观察简报），受 token 预算约束 |
| Agent 本体 | `agent_runtime.py`（`AgentRuntime` 协议、`BaseAgent`）+ `llm_agent.py` | Agent 的"决策逻辑"：看简报 → 出意图；规则型 vs LLM 型两套实现 |
| 记账本/待结算 | `transaction.py` 的 `TransactionBuffer` / `StagedEffect` / `EffectType` | 回合中段"先登记不生效"；回合末统一结算；冲突裁决（`resolve_conflicts`） |
| 慢通道 | `pending_ops.py`（`PendingOperationRegistry`）+ `executor_registry.py` + `llm_dispatcher.py` + `llm_gateway.py` | 跨回合的异步操作：LLM 思考、外部工具、人类审批——登记→执行→回投 |
| 世界物件 | `agent_tree.py`（组织树）、`task_tree.py`（任务树）、`private_store.py`（私有文件）、`shared_kb.py`（共享知识库+锁+版本）、`mailbox.py`/`outbox.py`（邮箱）、`human_control.py` | 世界的各种"东西" |
| 流水账与档案 | `journal.py`（TickJournal）+ `audit.py`（审计）+ `file_ops.py`（文件审计模型） | 每回合记账；审计是账本的视图 |
| 存档读档 | `persistence.py` + `Simulation.save_to` / `load_from` | 保存整个世界/恢复整个世界（SQLite 单事务） |
| 外壳 | `runtime.py`（`SimulationRuntime` 墙钟循环）+ `control_plane.py`（HTTP API） | 启动/暂停/单步/调速/查状态 |
| 支撑库 | `tool_manifest.py`（工具契约+生成 LLM 定义）、`tool_protocol.py`（请求/结果合约）、`sandbox_tools.py`/`python_worker.py`/`patch_ops.py`（受限执行）、`authority.py`（裁决原语）、`reliability.py`（超时/重试/锁租约）、`fake_llm.py`（假 LLM） | 小零件 |

## 二、一回合（`Simulation.run_tick`）逐阶段映射

十阶段序列：`ingest → freeze → schedule → observe → decide → validate →
act → commit → publish → audit`（SPEC §8.6；implement 中 deliver 在
schedule 前单独调用）。

| # | 通俗环节 | 代码（方法/模块） | 干什么 |
|---|---|---|---|
| 0 | 开账 | `_journal.start_tick(tick, epoch)` | 给本回合开流水账记录 |
| 1 | 收外围结果 | `_phase_ingest` | 收集外部结果（LLM 答完、工具跑完、人批完）：过期结果丢弃（fence）、超时唤醒；投递到期邮件走 `_phase_deliver` |
| 2 | 记局面（轻量） | `_build_snapshot` | 只建目录/元数据索引 + 摘要哈希（T17 起不含文件全文）——谁要读谁现取 |
| 3 | 叫醒 | `_phase_schedule` | 从事件队列选"该起床的人"（每 agent 每回合最多 1 次） |
| 4 | 每人看局面 | `_phase_observe` + `ContextCompiler.compile` | 给每个起床的 Agent 编"眼前所见"（任务/收件箱/KB/指标/工具列表） |
| 5 | 每人出招 | `_phase_decide` + `llm_agent` / `BaseAgent.decide` | 每个 Agent 做一轮决策：产出 `Intent` 列表 |
| 6 | 先审后办 | `_phase_validate` | 预审意图：工具权限（`ToolRegistry`）、委派合法性、payload、预算 |
| 7 | 登记 | `_phase_act` | 通过审核的意图 → `TransactionBuffer.stage`（本地效应）或 `PendingOperationRegistry.submit`（慢操作）；**绝不生效** |
| 8 | 统一结算 | `_phase_commit` | Commit-Validate（现在还能不能做？）→ 冲突裁决 → 逐条应用。**两级失败**：可判定失败（权限/锁/版本/patch 冲突）→ 该 effect 局部 FAILED、其余照常提交；仅 apply 抛未预期异常（系统级不变量破坏）→ 整回合回滚（T18 将重构为逐 effect 逆操作契约 + 显式失败分级） |
| 9 | 派发+公布 | `_phase_dispatch` / `_phase_publish` | 慢操作交给执行器（Executor Admission）、生成下一回合唤醒事件；**只有提交成功才派发** |
| 10 | 记总账 | `_phase_audit` | 本回合一切写进 Journal；审计事件是它的投影 |

## 三、慢通道：LLM 的思考要跨回合

Agent 想做的事分两种：**快事**（读文件、发信、记知识库——内核当场办，
`PURE/READ_ONLY/STAGED_MUTATION`）和**慢事**（LLM 思考、外部 API、人类
审批——要花真时间，不能让全世界等）。慢事像**点外卖**：Agent 下单
（`PendingOperationRegistry.submit`）、自己先挂起睡觉，外卖小哥
（`llm_dispatcher`/executor 后台执行）做好后，**下个回合**送到门口
（`_phase_ingest` 投递），Agent 醒来接着干。

**LLM ReAct 主链路（一个思考回合怎么走完）：**

最典型的慢通道就是 Agent 的思考，否则有的 Agent 思考时间长，有的 Agent
思考时间短，系统业务就不能实时推进了。

```text
tick t    Agent 唤醒 → 看简报 → 提交 LLM 请求 → 自己挂起（WAITING_FOR_LLM）
tick t+1  后台拨号（llm_dispatcher → llm_gateway），世界照常转
tick t+2  结果回来 → 验票（epoch / request_id）→ 投递唤醒 → Agent 续作
tick t+3  Agent 把结果转为下一批动作（工具/发信/再想）→ 一个 ReAct 回合结束
```

tick 是世界节拍，ReAct 回合**跨 tick**：等待时 Agent 不占世界，也**绝不
重复唤醒**——结果事件是唯一的闹钟（`_enqueue_result_wake`），没结果就没有
下一个回合。

**为什么安全（四行）**：提交成功才派发（回滚的回合单子作废，无幽灵单）；
回滚只撤本 tick 登记的 op（`remove_for_rollback`，request_id 可复用）；
结果回来必须验票（epoch/request_id 不符即丢——旧世界的答案进不来）；
超时给结构化错误，重试/放弃/升级由 Agent 自己决定。

**快慢的对称**：快通道副作用延后到 Commit（可回滚）；慢通道副作用在登记后
发生（不可回滚，靠补偿——T18 逆操作契约的另一半）。

## 四、世界存储（Object 群）

| 物件 | 类 | 关键点 |
|---|---|---|
| 组织树 | `AgentTree` | 谁是谁的上司/下属；静态 |
| 任务树 | `TaskTree` | 任务状态机、归属、依赖 |
| 私有空间 | `PrivateStore` | per-agent 真实目录；`resolve_path` 严防越界；读写按需（提交态 + 自己 staged，T17） |
| 共享知识库 | `SharedKB` | 文档条目 + `PermissionEngine` + `LockManager` + 乐观版本；锁已接入（T20）：kb 写工具自动持锁（Agent 无感）、commit 末释放、锁冲突可判定失败 |
| 邮箱 | `Mailbox` / `Outbox` | 收件箱 / 可靠投递（STAGED→COMMITTED→DISPATCHED，幂等） |
| 人类控制 | `HumanControl` | 人类暂停/发信/查状态（T12a 将扩展为 human worker） |

## 五、外壳与支撑

- `SimulationRuntime`（runtime.py）：墙钟后台线程跑 `run_tick` 循环
- `ControlPlane`：REST API（start / pause / step / status）
- `save_to` / `load_from`（Simulation）+ `persistence.py`：全组件 JSON blob
  单事务存档
- `fake_llm`：脚本化假 LLM（测试/演示，无真实模型）
- `authority.py`：Authority 裁决原语（扩展表面三查分离的裁判核心，当前
  独立未接线）
- `reliability.py`：超时 / 重试 / 锁租约 / 确定性重放
- `tool_manifest.py`：工具契约 + `manifest_to_tool_definition`（T7，LLM
  工具定义的唯一来源）

## 六、诚实边界（读代码时注意）

1. **`Simulation` 是上帝对象**：调度、观察、结算、回滚都住在它里面。
   T17（快照按需化）与 T18（回滚逆操作契约）已完成，正在拆薄；
   但主体拆分（抽独立服务）短中期不做。
2. **Journal 尚未完全兑现"单一事实源"**：`_phase_commit` 先改内存再记
   Journal；持久化走 `_collect_state` 全量序列化（`save_to`）。当前是
   "Journal 记账 + 快照存档"双轨。回滚靠逆操作（T18）、重放靠 Journal、
   恢复靠存档——三者是邻接议题，统一时再收敛。
3. **外部进程工具（run_tests / git）的 cwd 当前指向宿主目录**，未接到
   agent workspace——归"工具执行环境对齐"卡（v0.10 次优先级），读代码
   时勿误以为它们已作用于模拟世界。
4. **pending_ops 无显式同步锁**：目前靠 GIL + 单后台写者（`llm_dispatcher`
   线程）与主线程读维持安全；复合状态转换（检查+改状态）非原子，窗口
   极小但存在——未来多 worker 并发回写（外部执行器）**必须先加锁**。