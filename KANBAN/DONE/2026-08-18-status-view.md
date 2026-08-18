---
kind: task
status: completed
phase: 4 - 人类控制
source: SPEC §16.5, §16.6, §16.7
---

# 状态查看

**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

- `src/my_team/human_control.py` — 多个 view_* 方法
- view_simulation_status / view_agent_tree / view_task_tree
- view_locks / view_agent_status

### 验收标准

- [x] 能查看完整组织树
- [x] 能查看任务树及状态
- [x] 能查看当前活跃锁
