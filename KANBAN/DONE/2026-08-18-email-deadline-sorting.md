---
kind: task
status: completed
phase: 2 - Email Collaboration
source: "SPEC §13.3; review #15; report §7 P2"
priority: medium
---

# Email Deadline Sorting (§13.3)


## 目标

在邮箱排序中加入 `deadline_tick`，实现完整 SPEC §13.3 合规。

## 背景

当前 `_sort_key` 返回 `(type_rank, priority_rank, created_at_tick, email_id)`。SPEC §13.3 推荐的排序包含"截止时间更近的任务优先"，当前实现缺少此维度。

## 要求

1. `Email` 模型关联 `task_id` → 解析任务 `deadline_tick`
2. `_sort_key` 加入 deadline 维度：`deadline_tick` 越小越优先
3. 无 deadline 的邮件排在有 deadline 之后
4. 更新 mailbox.py 和对应测试

## 产出

- [ ] 修改 `mailbox.py` 的 `_sort_key`
- [ ] 添加 deadline 排序测试
- [ ] 更新 SPEC §13.3 合规声明

## 验收标准

- [ ] 有 deadline 的邮件排在无 deadline 之前
- [ ] deadline 更近的邮件排在更远的之前
- [ ] 所有现有邮件测试仍通过
