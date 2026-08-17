# Pause Semantics Documentation

**Phase:** 4 - Human Control
**Source:** review #17; report §4.14
**Priority:** P2 — Documentation

## 目标

明确 Pause 期间的锁租约、任务 deadline 和邮件投递语义。

## 背景

当前行为"碰巧正确"——Pause 时 tick 不推进，所以锁和 deadline 不会过期。但这些语义未被显式文档化和测试。

## 要求

1. 文档化规则："所有任务 deadline 和锁 lease 只基于 simulation tick；paused 时不发生任何基于 tick 的过期；恢复后从下一个 phase 边界继续"
2. 添加测试验证：
   - Pause 期间锁不释放
   - Pause 期间任务不超时
   - Pause 期间邮件不投递
   - Resume 后从正确 phase 继续
3. 添加 `test_pause_lock_deadline.py`

## 产出

- [ ] 更新 `human_control.py` 文档注释
- [ ] 添加 pause 语义测试

## 验收标准

- [ ] Pause 期间锁 lease 不推进
- [ ] Pause 期间任务 deadline 不推进
- [ ] Resume 后从正确的 phase 边界继续
