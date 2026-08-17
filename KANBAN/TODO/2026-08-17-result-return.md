# 结果返回机制

**Phase:** 2 - E-mail 协作
**Source:** SPEC §7.4

## 目标

实现子 Agent 完成任务后向上级返回成果的机制。

## 返回方式

通过 `result` 类型 E-mail 返回，包含:

- summary: 摘要
- artifacts: 成果引用（路径 + 版本号）
- limitations: 局限性
- recommendation: 建议
- status: submitted / failed

## 规则

- E-mail 正文放: 摘要、结论、风险、待决策事项
- 大体积内容写入私人空间或共享知识库
- E-mail 中只传递引用、路径和版本号

## 产出

- result 邮件生成器
- 成果引用解析
- 上级汇总逻辑

## 验收标准

- [ ] 子 Agent 能发送 result 邮件
- [ ] 成果引用包含正确的路径和版本号
- [ ] 上级 Agent 能解析 result 并更新任务状态
