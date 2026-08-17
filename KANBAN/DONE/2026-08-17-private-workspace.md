# 私人工作空间初始化

**Phase:** 1 - 基础运行时
**Source:** SPEC §5.1, §5.2
**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

### 产出文件

- `src/my_team/private_store.py` — 私人工作空间管理
- `tests/test_private_workspace.py` — 29 个测试用例（含 file-ops），全部通过

### 实现的功能

1. **目录初始化**: `initialize_agent()` / `initialize_all()` 为每个 Agent 创建 inbox/, outbox/, workspace/, memory/, task_state/, logs/
2. **路径解析**: `resolve_path()` 安全解析相对路径，阻止路径穿越攻击
3. **访问控制**: `check_access()` / `assert_access()` 确保 Agent 只能访问自己的空间
4. **存储统计**: `get_storage_usage()` 统计 Agent 空间使用量
5. **配置化**: 支持自定义基础路径、文件大小限制、存储配额

### 验收标准

- [x] 创建模拟时自动为所有 Agent 生成目录结构
- [x] 访问控制阻止跨 Agent 读写
- [x] Agent 无法 ls 其他 Agent 的空间
