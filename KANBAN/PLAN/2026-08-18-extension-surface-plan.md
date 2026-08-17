# Extension Surface 实现计划（v0.11 → v1.0）

**Date:** 2026-08-18
**Kind:** plan
**Phase:** v0.11 扩展表面
**Source:** `OPEN_ISSUE/2026-08-18-extension-surface-spec.md` 全面审阅结论
**Status:** 计划中

## 定位

把 OPEN_ISSUE 里已收敛的架构原则（四层 + 闭包、Authority 8 域/七元组、
治理图六关系、四条不变量、静态校验器边界）**落成可执行的运行时语义**。
目标不是"再增加抽象概念"，而是让扩展表面成为一份真正的合同：
任何可变数据都有明确的版本、生命周期、权限、恢复、可重放语义。

## 最小测试向量（贯穿所有任务的唯一判据）

```text
IngressEvent → ProcessInstance → assignment → human approval
→ external irreversible operation → crash recovery
→ compensation / reconciliation
```

这条链路在语义上闭合之前，不实现任何场景资产（软件公司/小说工作室/电商/
自媒体/知识星球）。

## P0 任务（阻塞层，见 TODO/ 各文件）

| # | 任务 | 对应审阅缺口 | 产出 |
|---|---|---|---|
| 1 | process-model | P0-1 + §四 ProcessInstance/Task 关系 + HumanTask | ProcessDef/ProcessInstance/Gate schema + 状态机 |
| 2 | authority-evaluation | P0-5 + §三 claim/context/composition | DecisionClaim + 裁决算法 |
| 3 | pending-outbox-recovery | P0-2 + P0-3 + P0-7 | pending op 完整生命周期 + outbox 恢复 + unknown/对账 |
| 4 | execution-profile | P0-4 + §五 effective_tick | ExecutionProfile + 版本绑定 |
| 5 | package-trust-boundary | P0-6 + §七 Skill 信任 | 三类资产边界 + 签名/命名空间 |
| 6 | predicate-dsl | P0-8 + §四.2 | 谓词 L0/L1/L2 边界 |
| 7 | static-validator | §八 五类 + 可达性/终局/单调/数据流/资源 | 校验规则清单 + validator |

## 依赖关系

```text
process-model ─────────────┐
authority-evaluation ──────┼──→ static-validator
pending-outbox-recovery ───┤         ↑
execution-profile ─────────┤         │
package-trust-boundary ────┤         │
predicate-dsl ─────────────┘         │
                                     │
execution-profile / package-trust ───┘
```

- `process-model` 是其余任务的对象载体（ProcessInstance 绑定 profile、authority、
  pending op）；先行。
- `authority-evaluation` 与 `pending-outbox-recovery` 可并行（前者管裁决，
  后者管可靠性）。
- `execution-profile` 依赖 `process-model`（profile 被 ProcessInstance 引用）。
- `package-trust-boundary` 与 `predicate-dsl` 独立，可并行。
- `static-validator` 收尾：依赖前六者的 schema 定型（校验的是它们的良构性）。

## P1 backlog（进入 v1.0 前，未拆 TODO）

1. 包签名、依赖、命名空间、api_compatibility 完整化（T5 已含骨架）。
2. 并行分支 / join / 重试 / 超时 / 取消 / 补偿的运行时语义（T1 已含状态机，
   此为实现）。
3. root 自进化的风险分级（低/中/高：谁可自动灰度、谁须 Owner 审批）；
   **authorization 永不扩大、闭包永不可改、裁决链不可绕过 Owner**。
4. Skill 的 prompt 注入隔离落到 ContextCompiler（结构化来源段）。
5. `effective_tick` 发布语义落到 runtime（T4 已含 schema）。
6. 静态校验器接入 INSTALL_PACKAGE 加载路径 + 生成时（root 产物）路径。

## P2 backlog（后续增强）

子流程/subgraph 嵌套 · route 灰度与 A/B · ProcessInstance 显式迁移 ·
多 Deployment 扩展 · embedding 型 KB 检索 · Authority 动态条件与 threshold。

## 验收门（每项对应审阅"验收不变量"）

- [ ] 任一 ProcessInstance 的全部运行时语义绑定到一个不可变 ExecutionProfile
- [ ] 新 PackageVersion 发布不改变既有 ProcessInstance 结构
- [ ] Commit 成功但 Publish 未完成时，重启后可恢复 outbox（稳定幂等键）
- [ ] 外部调用结果为 `unknown` 时，未经确认不重复执行不可逆操作
- [ ] 任意 gate 的拒绝/超时/未决都有终止路径或显式 escalation
- [ ] 任意 predicate 纯、有限、可审计、可重放
- [ ] root 不能扩大自身 authorization，不能绕过 Owner escalation
- [ ] 配置包不能携带未声明/未签名/未授权的可执行能力
- [ ] 同 Deployment 内私有记忆/凭证/资产引用不交叉泄漏
- [ ] 角色变化不伪造/覆盖 Principal Identity
- [ ] 外部事件重复投递不创建重复 ProcessInstance（除非流程显式声明允许）
- [ ] 任一 effect 可追溯到 Intent、ProcessInstance、PackageVersion、Principal
- [ ] 最小测试向量端到端闭合，内核代码零改动
