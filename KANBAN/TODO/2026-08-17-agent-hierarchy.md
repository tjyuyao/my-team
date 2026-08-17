# Agent 组织树定义与加载

**Phase:** 1 - 基础运行时
**Source:** SPEC §2.2, §4.1, §11.1, §17

## 目标

实现静态 Agent 组织树的定义、加载和校验。

## 要求

- 从 JSON 配置文件加载 Agent 树
- 每个 Agent 包含: agent_id, display_name, role, parent_id, children, tools, permissions
- 校验不变量:
  - 一个 Agent 只有一个父 Agent（根 Agent 除外）
  - 组织关系不能形成环
  - Agent 只能向直接子 Agent 委派
- 运行期间组织树不可变

## 产出

- Agent 树配置 schema
- 加载与校验逻辑
- 树遍历工具（查找父/子/兄弟节点）

## 验收标准

- [ ] 能从配置文件正确加载 6 个 Agent 的树结构
- [ ] 环形引用检测报错
- [ ] 重复 agent_id 检测报错
- [ ] 树遍历 API 正确返回父节点和子节点列表
