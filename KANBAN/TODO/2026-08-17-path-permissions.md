# 路径权限控制

**Phase:** 3 - 共享知识库
**Source:** SPEC §6.2

## 目标

实现基于路径、Agent 和操作的权限控制模型。

## 权限操作

```text
list / read / create / write / append
rename / delete / lock / unlock / publish
```

## 权限规则示例

```json
{
  "scope": "project/research/*",
  "principal": "agent.research",
  "allow": ["list", "read", "create", "write", "append", "lock", "unlock"]
}
```

## 最小权限原则

- 研究 Agent 只能修改 `project/research/*`
- 规划 Agent 只能修改 `project/planning/*`
- 审查 Agent 对成果目录只读和审查
- Root Agent 可读取所有项目目录
- `decisions/*` 只有 Root Agent 或授权审查 Agent 可发布

## 产出

- 权限规则配置
- 路径匹配引擎
- 操作级权限检查
- 权限拒绝审计

## 验收标准

- [ ] Research Agent 不能写入 `project/planning/*`
- [ ] 权限拒绝产生审计日志
- [ ] 权限规则支持通配符匹配
