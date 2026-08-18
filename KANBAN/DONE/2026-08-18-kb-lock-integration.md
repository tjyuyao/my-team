---
kind: task
status: completed
phase: v0.10 能力
source: SPEC §6（SharedKB 锁）；用户 2026-08-18 审查发现
priority: high
---

# v0.10-20: KB 锁接入（kb-lock-integration）

**排期：v0.10，与 T18 同批**（锁是"每 tick 一轮 + 互斥锁 = 串行化"原则的
半边，必须从纸面变现实；T18 的失败分级是它的语义前提）。

## 背景（审查发现 2026-08-18）

`LockManager`（shared_kb.py）实现正确且有测试（lease 租约、过期自动释放、
lock_token 防 stale-holder）——但 **src/ 生产代码零调用**：`acquire` 只有
测试直接调。后果链：

1. `handle_kb_write` 不 acquire、不携带 lock_token，直接 stage KB 写 effect；
2. 提交时 `check_lock`（simulation.py）对 KB 资源强制"必须持锁且 owner 匹配"
   → 无锁即拒绝（validate 阶段 FAILED）；
3. 即便绕过 validate，`SharedKB._apply_committed` 也强制 "Must hold lock to
   write"。

**结论：真实 Agent 流程中 kb_write/kb_create/kb_delete 写 KB 必然被拒**。
"锁=串行化"的锁半边是纸面承诺；KB 写工具从未真正可用。

## 设计决策（已定，勿在执行时重开）

1. **acquire 在 Act 阶段同步执行**（工具 handler 内）：`LockManager.acquire
   (resource, agent_id, current_tick)`；`LockConflictError`（已锁且租约未到期）
   → 当场返回**可判定失败**（ToolResult success=False + 错误消息），Agent
   下回合重试（每 tick 一轮语义下天然轮询，配合 lease 过期可抢）。
2. **lock_token 随 effect 传递**：acquire 得到的 token 存入 StagedEffect 的
   lock_token 字段 → `check_lock` 校验 token 匹配（逻辑已存在，simulation.py
   check_lock 3248-3250，补上 token 来源即可）。
3. **释放时机**：effect 应用成功后立即 release（写事务提交即释，防锁滞留）；
   **回滚时释放本 tick 所有 acquire 的锁**（新增 rollback 步骤，修复
   `_rollback` 不恢复锁的缺口——现靠 lease 过期兜底，接入后改为显式释放）。
4. **不引入等待队列**：v1 不做 WAITING_FOR_LOCK 排队（AgentState 已有该状态
   但未用），靠"失败→下回合重试"；排队留给未来设计（慢通道语义）。
5. **争锁裁决**：Act 阶段串行执行（_phase_act 循环），同 tick 多 agent 争锁
   先到先得；后者工具失败；版本检查（乐观并发第二道防线）照旧。
6. **锁对 Agent 不可见（无显式锁工具）**：acquire/release 由写工具内核
   绑定（写 KB = 自动持锁），Agent 无 lock/unlock 概念 → **没有"忘记释放"
   的遗忘面**（用户 2026-08-18 权衡确认）。残留窗口仅单 tick
   （acquire→commit），极端滞留由 lease 兜底（默认 4 tick 自动过期）。
   若未来需要跨 tick 长持锁（如多轮协作编辑同一文档），再引入显式
   lock/unlock 工具 + "邮件询问资源是否仍在使用"的辅助手段（记 backlog，
   不属本卡）。

## 实施步骤
1. `shared_kb.py`：确认 acquire 的 LockConflictError 语义与 lease 默认值；
   补 acquire 后的 owner/token 查询便利方法（如需要）。
2. `simulation.py`：`handle_kb_write`（及未来 kb_create/kb_delete handler）
   acquire + token 传递；Commit 的 KB apply 分支成功后 release；`_rollback`
   释放本 tick 获取的锁。
3. 失败分级：acquire 冲突 = 可判定失败（T18 语义），绝不经由异常触发整回合
   回滚。
4. 测试：kb_write 端到端成功（真实 tick 流程，不再被拒）；两 agent 争同一
   KB 资源（先到先得、后者重试成功、lease 过期可抢）；回滚释放锁；锁审计。

## 验收标准
- [ ] 真实 tick 流程中 kb_write 能成功提交（此前必被拒）
- [ ] 同一资源并发：先到先得；后者拿到可判定失败并在后续 tick 重试成功
- [ ] lease 过期后其他 Agent 可获取锁（无死锁、无滞留）
- [ ] 回滚释放本 tick 获取的锁
- [ ] 锁获取/释放/冲突进入审计
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过；kanban_lint 0 violation
## 完成注记（2026-08-18，与 T18 同批）

实现要点：
- `handle_kb_write`（simulation.py）：permission 预检 → `acquire`（
  `LockConflictError` → 可判定 `LOCK_CONFLICT` 失败，retryable=True + 审计）
  → `lock_token` 随 StagedEffect 传递（补上 validate check_lock 的 token 来源）
  → commit 末统一释放。
- `_tick_acquired_locks` 簿记（`_phase_act` 清空，handler 追加）；新
  `_release_tick_locks()` 幂等释放（已释放跳过，错误吞，lease 仅兜底），
  成功路径 commit 末调用、回滚路径 `_rollback` 先行调用。
- **释放时机 = commit 末（非逐 effect）**：同 tick 同 agent 多次写同一资源
  不因中途释放而二次失败（`_apply_committed` 强锁检查贯穿）；锁绝不跨 tick
  存活，租约只是兜底——决策 3"写事务提交即释"以防滞留为目标的精化。
- 锁对 Agent 不可见（无显式锁工具）：acquire 只在写工具内核绑定；同一资源
  同 tick 争锁由 Act 串行先到先得，后者可判定失败、下 tick 重试成功（lease
  过期天然可抢）。
- 审计：LOCK_ACQUIRED / LOCK_CONFLICT / LOCK_RELEASED 入 audit。
- 测试：kb_write 真实 tick 流程成功（去掉测试手工预持锁）；同资源两 agent
  争锁（先到先得 + LOCK_CONFLICT + 下 tick 重试成功 + 锁生命周期审计）；回滚
  释放 handler 获取的锁；锁不跨 tick 滞留。全量 792 passed；mypy clean；
  ruff 通过；kanban_lint 0 violation。
- 文档：SIMULATION_MAP 共享知识库行锁缺陷标注更新为"已接入（T20）"。

## 验收核对
- [x] 真实 tick 流程中 kb_write 能成功提交（此前必被拒）
- [x] 同一资源并发：先到先得；后者拿到可判定失败并在后续 tick 重试成功
- [x] lease 过期后其他 Agent 可获取锁（无死锁、无滞留；锁 commit 末即释，lease 仅兜底）
- [x] 回滚释放本 tick 获取的锁
- [x] 锁获取/释放/冲突进入审计
- [x] `uv run pytest -q` 792 passed；`ruff`/`mypy` 通过；kanban_lint 0 violation
