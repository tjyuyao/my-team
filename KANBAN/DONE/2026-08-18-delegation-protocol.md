---
kind: task
status: completed
phase: 2 - E-mail 协作
source: SPEC §7.1, §7.2, §7.3
---

# 委派协议

**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

### 产出文件

- `src/my_team/delegation.py` — DelegationProtocol 完整实现
- `tests/test_phase2.py` — 36 个测试用例，全部通过

### 实现的功能

1. **delegate()**: 创建子任务 + 发送委派邮件
2. **accept() / reject()**: 接受/拒绝委派
3. **report_progress() / report_blocked()**: 进度/阻塞报告
4. **submit_result()**: 提交工作成果
5. **cancel()**: 取消任务
6. **约束验证**:
   - 只能委派给直接子节点 ✓
   - 子任务截止时间不超过父任务 ✓
   - 委派深度限制 ✓
7. **check_expired()**: 检查并标记过期任务

### 验收标准

- [x] Root Agent 能委派给 Research Agent
- [x] Research Agent 能继续委派给 Web Research Agent
- [x] Web Research Agent 不能委派给 Planning Agent
- [x] 委派授权满足: 子 Agent 有效权限 ⊆ 委派者有效权限
