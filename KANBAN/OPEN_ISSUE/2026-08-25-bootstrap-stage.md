---
kind: issue
status: open
source: grill-me 会话（2026-08-25）Q3/Q6；SPEC §0.2/§4.1/§5.8
priority: medium
---

# 引导阶段：Root 亲力亲为 → 逐步 hire → 组织生长

**Opened:** 2026-08-25
**Status:** OPEN — 方向已定，机制未设计，属未来阶段（非 v0.11 范围）

## 背景

「自举」是 My-Team 的关键愿景：非专业 Owner 装好系统后，无需手写
配置/SOP/授权，由 Root（职业经理人 Agent）先**亲力亲为**操持核心
业务，再逐步「hire/create」下属 Agent、慢慢扩展组织架构。这与人类
创业公司的真实成长路径同构，也是对「面向无软件开发背景个体户」定位
的落地回答。

## 已收敛的方向（来自 grill，写入 SPEC 的部分见对应章节）

1. **Root 先亲自操持核心业务，不急于建组织架构**；组织从单节点有机
   生长，而非 Day 1 预配置完整岗位树。
2. **Root 在已注册能力空间内自由重组**（创建 Position、建议 Grant、
   起草 SOP）；**新的不可逆路径引入（新 Integration/工具接入）原则
   上需 Owner 审批**（或调试运行给假接口）。
3. 外部接口甚至可由 Root 根据非 My-Team 生态 Provider 的文档实现，
   但需 Owner 审批；需人类亲自办理的材料，经邮件要求 Owner 或其指派
   员工补齐。
4. 设备对 Root 隐藏实现细节，只暴露「特定的签名版本」提供权限。

## 开放问题（待议，暂不拆卡）

- [ ] Root 的「自举配置」边界：Root 能否改写 Authority Grant？能否
      创建新 Position 并自我挂载？「授权永不扩大」与「Root 自主调整
      组织」如何精确调和（P1 backlog#3 已有方向，此处求机制）。
- [ ] 引导阶段的触发与终止：何时判定「该 hire 下属了」？Root 因单
      activation 瓶颈（§3.1）感到压力 → 主动提议扩编的判定标准。
- [ ] Owner 审批的粒度：Root 自主 vs Owner 审批的边界如何随组织成熟
      度**动态放宽**（初期审批多、稳定后放权多）——对应「允许犯错、
      错误中改进」的信任递进。
- [ ] Day 1 安装体验：引导阶段作为默认入口时，Owner 首次交互的最小
      闭环是什么。
- [ ] 与「Root 自进化风险分级」（P1 backlog#3）的关系：自举是自进化
      的**组织维度**，需与流程/能力维度统一到同一治理闭环。

## 备注

- 本议题是设计方向沉淀，不阻塞 v0.11 语义闭合；在合适的未来阶段
  （v1.0 前）再拆 TODO 落位。
- 相关既有卡/议题：`KANBAN/PLAN/v0.11.0-plan`（P1 backlog#3 自进化）、
  `KANBAN/OPEN_ISSUE/extension-surface-spec`（root 自进化元编程）。
