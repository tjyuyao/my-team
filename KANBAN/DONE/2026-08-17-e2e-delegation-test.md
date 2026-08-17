# 端到端委派测试

**Phase:** 6 - 系统集成
**Source:** SPEC §7, §20
**Priority:** P0
**Review ref:** 差距 §8.3

## 目标

验证完整的委派-执行-返回链路在系统中真正工作。

## 流程

```text
Root 创建任务
→ Root 委派给 Research (delegation email)
→ Research 接受 (acceptance email)
→ Research 委派给 WebResearch (delegation email)
→ WebResearch 执行，写入共享 KB
→ WebResearch 返回 result (result email)
→ Research 收到 result，汇总
→ Root 收到最终 result
```

## 验收标准

- [ ] Root Agent 能创建任务并委派
- [ ] 子 Agent 能通过邮件接收委派
- [ ] 子 Agent 能继续委派给自己的子 Agent
- [ ] 共享 KB 写入正确执行
- [ ] result 邮件正确返回
- [ ] 所有邮件在审计日志中有记录
- [ ] 任务状态转换完整覆盖
