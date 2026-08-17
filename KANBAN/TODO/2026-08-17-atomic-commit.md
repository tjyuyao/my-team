# 原子提交事务

**Phase:** 6 - 系统集成
**Source:** SPEC §8.2 Phase 6, §13.2
**Priority:** P1
**Review ref:** 差距 §8.4

## 目标

实现 Tick Commit 阶段的完整事务模型。

## 要求

```text
Observe: 读取 immutable snapshot
Decide: 生成 ActionPlan
Act: 只产生 staged effects，不修改全局状态
Commit:
  1. 验证所有前置条件（权限、锁、版本）
  2. 按确定性顺序解决资源冲突
  3. 原子提交成功动作
  4. 失败动作整体回滚或明确标记
```

### 需要测试的场景

- 两个 Agent 同时申请同一锁
- 两个 Agent 同时提交同一版本文件
- 一个 Agent 发邮件同时更新任务
- 一个 Agent 在提交过程中失败
- 重放结果是否完全一致

## 产出

- `src/my_team/transaction.py`
- `tests/test_atomic_commit.py`

## 验收标准

- [ ] Act 阶段不修改全局状态
- [ ] Commit 阶段按确定性顺序提交
- [ ] 冲突动作被正确拒绝或回滚
- [ ] 部分失败不影响其他 Agent 的成功动作
- [ ] 重放得到一致状态
