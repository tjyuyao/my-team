# 私人工作空间初始化

**Phase:** 1 - 基础运行时
**Source:** SPEC §5.1, §5.2

## 目标

为每个 Agent 创建独立的私人工作空间目录结构。

## 要求

每个 Agent 的私人空间包含:

```text
/private/
  agent.{id}/
    inbox/       # 收件箱
    outbox/      # 发件箱
    workspace/   # 工作文件
    memory/      # 私密记忆
    task_state/  # 任务状态
    logs/        # 运行日志
```

## 访问规则

- Agent 只能访问自己的私人空间
- Agent 不能列出其他 Agent 的私人空间
- Agent 不能读取其他 Agent 的私密记忆
- 父 Agent 不能绕过 E-mail 读取子 Agent 的私人文件
- 子 Agent 不能绕过 E-mail 直接修改父 Agent 的空间

## 产出

- 目录初始化逻辑
- 路径解析与访问控制
- 运行时目录创建

## 验收标准

- [ ] 创建模拟时自动为所有 Agent 生成目录结构
- [ ] 访问控制阻止跨 Agent 读写
- [ ] Agent 无法 ls 其他 Agent 的空间
