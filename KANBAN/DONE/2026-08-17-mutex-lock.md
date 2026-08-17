# 排他锁

**Phase:** 3 - 共享知识库
**Source:** SPEC §6.3
**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

### 产出文件

- `src/my_team/shared_kb.py` — LockManager + LockInfo
- `tests/test_phase3.py` — 41 个测试用例，全部通过

### 实现的功能

1. **LockManager**: 排他锁管理器
2. **acquire/release/renew**: 完整锁生命周期
3. **租约超时**: 可配置默认租约时长
4. **自动释放**: 过期锁自动标记，允许重新获取
5. **冲突检测**: 同一资源不能同时被两个 Agent 持有

### 验收标准

- [x] 同一资源不能同时被两个 Agent 持有排他锁
- [x] 租约到期自动释放
- [x] 锁冲突产生系统通知邮件
