---
kind: task
phase: v0.11 post-agent
source: 审阅 P0-6、§七；OPEN_ISSUE 能力层；SPEC §11/§11.2；2026-08-24 信任模型决策（审计制）
priority: high
---

# 资产校验与审计边界：三类资产 + 声明诚实性 + 安装审计
> **废弃（2026-08-25，Owner 定）**：v0.11 计划整体归档（见
> `KANBAN/PLAN/2026-08-25-v0.11.0-plan.archived.md`），本卡随计划
> 一并废弃。原因：重构方案重议——原「增量功能 + 事后结构重排」
> 路线不满足三态质量前提（测试绿 ≠ 结构正确）。本卡内容留档备查，
> 不执行；新方案见 `docs/THREE_STATE_REFACTOR_PLAN.md`。



## 目标
让包系统在**不设信任仪式**的前提下保持可治理。信任模型为审计制
（2026-08-24 Maintainer 决策）：无签名门槛、无分发期（产品形态一客一实例，
扩展直接装进客户实例，无第三方包市场）。安全机制 = **如实声明 +
安装审计 + 运行时约束（沙箱分级 + deny-by-default）+ 审计员事后
审查**。三类资产的区分保留，语义从"信任等级"改为**"校验深度 +
审计要求"**。

## 决策记录（2026-08-24，Owner）
- 可执行内容不需要事前密码学信任（签名链），只要可审计即可。
- 安全由**审计员**负责：Owner 本人或其指定 kind=human 成员，事后审查
  安装记录与行为审计。
- 本项目扩展无分发期：一客一实例。原「B 类仅 Maintainer 安装 + 签名」
  表述作废（OPEN_ISSUE 已加同日决策注记）。
- 场景包定位修订（同日）：**完全扩展能力的自包含单元**，可自带
  ToolPlugin 定义的工具；「对既有能力包引用为主」作废，独立能力包
  为跨场景复用的可选形态。

## 要求 / 规则
- 三类资产（校验/审计档案不同，非信任等级）：
  - **A 声明类**（Org/Role/ProcessDef/Authority/Schedule/KPI/路由）：
    全量静态校验（E7 五类基础 + 补五类）；root 可提议；
  - **B 可执行类**（Tool handler/Integration adapter/MCP adapter/
    Skill script/受控脚本）：静态校验器不查代码行为，代之以：
    manifest 如实声明（capabilities_requested + 执行器分级）+
    运行时沙箱分级 + OperationPolicy deny-by-default；
  - **C 数据类**（KB seed/模板/示例数据）：内容默认不可信数据，
    注入须带来源标签（SPEC §11.4）。
- **声明诚实性是硬约束**：包内可执行条目未在 manifest 声明（缺
  capabilities_requested 或执行器分级）→ 加载即拒。这不是信任门槛，
  是审计完整性要求——审计员无法审查未声明的东西。
- **安装 = 审计事件**：INSTALL_PACKAGE 事务 effect 入 Journal，记录
  installer、content_hash（防篡改比对）、capabilities_requested、
  执行器分级；能力类条目安装同时通知审计员（复用 outbox 邮件模型）。
- 包 schema：`package_id / version / api_compatibility / content_hash /
  installer / dependencies / capabilities_requested / namespace`
  （原 signer 字段改 installer；不建签名验证机制）。
- 命名冲突处理不变：显式报错，不静默覆盖。
- 能力载体两路**平权**（2026-08-24 定位修订）：随场景包/Skill 内嵌，
  或独立成能力包供跨场景复用——仅为复用考量，运行时权限无差异。
  ToolPlugin 定义的工具与内核内置工具同权（进程内注册），约束面 =
  如实申报 + 安装审计 + OperationPolicy deny-by-default。
- 提示词注入隔离不变（落到 ContextCompiler）：结构化来源段 + 优先级。

## 产出
- 三类资产的校验/审计档案 spec + 包 schema（installer/content_hash）。
- 安装审计事件 + 审计员通知流（复用邮件模型）。
- 命名冲突规则；Skill 来源段信任等级规则（供 ContextCompiler 实现）。

## 验收标准
- [ ] 未如实声明的可执行条目加载即整体拒绝（结构化错误，有测试）
- [ ] 任一能力安装可在 Journal 追溯 installer / content_hash /
      能力声明 / 执行器分级
- [ ] 能力类安装触发审计员通知
- [ ] 内嵌可执行条目的包与独立能力包运行时权限无差异
      （同沙箱分级、同 deny-by-default，有测试）
- [ ] 工具名/role 名/record schema 冲突显式报错，不静默覆盖
- [ ] Skill 的 prompt/KB/payload 带来源与信任等级；客户内容不作指令
- [ ] 最小测试向量段：非法包被整体拒绝（不半装）通过
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过
