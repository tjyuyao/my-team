# Agent 组织树定义与加载

**Phase:** 1 - 基础运行时
**Source:** SPEC §2.2, §4.1, §11.1, §17
**Status:** DONE
**Completed:** 2026-08-17

## 完成内容

### 产出文件

- `src/my_team/models/agent.py` — Agent 配置数据模型 (Pydantic)
- `src/my_team/agent_tree.py` — 组织树加载、校验、遍历
- `tests/test_agent_hierarchy.py` — 30 个测试用例，全部通过

### 实现的功能

1. **配置加载**: 支持从 JSON 文件和字典加载 Agent 树
2. **不变量校验**:
   - 重复 agent_id 检测 ✓
   - 环形引用检测（parent 链 + children 链双向检查）✓
   - 多根节点检测 ✓
   - 无根节点检测 ✓
   - 父子一致性校验 ✓
   - 声明的子节点必须存在 ✓
3. **树遍历 API**:
   - `get(agent_id)` — 获取 Agent 配置
   - `children(agent_id)` / `child_ids(agent_id)` — 直接子节点
   - `parent(agent_id)` — 父节点
   - `siblings(agent_id)` — 兄弟节点
   - `is_ancestor(a, b)` — 祖先关系判断
   - `ancestors(agent_id)` — 所有祖先
   - `depth(agent_id)` — 深度
   - `subtree_ids(agent_id)` — 子树所有 ID
   - `can_delegate_to(delegator, target)` — 委派权限检查
4. **序列化**: `to_dict()` 支持往返序列化

### 验收标准

- [x] 能从配置文件正确加载 7 个 Agent 的树结构
- [x] 环形引用检测报错
- [x] 重复 agent_id 检测报错
- [x] 树遍历 API 正确返回父节点和子节点列表
