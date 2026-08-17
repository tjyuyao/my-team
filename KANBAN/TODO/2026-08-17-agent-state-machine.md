# Agent 状态机

**Phase:** 1 - 基础运行时
**Source:** SPEC §9

## 目标

实现 Agent 生命周期状态机。

## 状态定义

```text
created → initialized → ready → running → terminated

running 子状态:
  idle        - 无待处理任务，可接收 E-mail
  processing  - 正在处理当前时间步输入
  waiting     - 等待子 Agent / E-mail / 锁 / 外部事件
  blocked     - 无法继续，需上级或人类介入
  paused      - 系统暂停或策略原因停止
  failed      - 执行失败，可重试或恢复
```

## 状态转换规则

- idle → processing: 收到新邮件或任务
- processing → waiting: 需要等待外部响应
- processing → blocked: 无法自行解决
- waiting → processing: 收到响应
- blocked → idle: 上级介入解决
- 任意 → failed: 执行异常
- failed → idle: 重试成功
- failed → terminated: 重试耗尽

## 产出

- 状态枚举与转换表
- 状态转换 API
- 状态变更审计

## 验收标准

- [ ] Agent 按规则在状态间转换
- [ ] 非法转换被拒绝
- [ ] 所有状态变更写入审计日志
