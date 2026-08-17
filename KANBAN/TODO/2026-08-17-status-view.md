# 状态查看

**Phase:** 4 - 人类控制
**Source:** SPEC §16.5, §16.6, §16.7

## 目标

实现人类查看系统状态的 API。

## 查看接口

- `GET /simulations/{id}/agents/tree` - 组织树
- `GET /simulations/{id}/tasks/tree` - 任务树
- `GET /simulations/{id}/shared-kb/locks` - 共享知识库锁

## 状态信息

- Agent 当前状态和角色
- 任务进度和状态
- 邮箱中的邮件
- 共享知识库锁状态
- 当前时间步
- 系统是否暂停

## 产出

- 状态查询 API
- 树形结构序列化
- 锁状态查询

## 验收标准

- [ ] 能查看完整组织树
- [ ] 能查看任务树及状态
- [ ] 能查看当前活跃锁
