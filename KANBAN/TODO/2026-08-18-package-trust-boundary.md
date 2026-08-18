---
kind: task
phase: v0.11 扩展表面
source: 审阅 P0-6、§七；OPEN_ISSUE 能力层；SPEC §11
priority: high
---

# 配置包 / 能力包 / 数据包信任边界 + 签名 / 命名空间


## 目标
"热插拔加载配置包"与"热插拔加载可执行代码"是两种完全不同的安全问题。
当前 `scenario/tools/` 的 manifest + handler/executor 引用会让任何配置包
都能携带代码。本任务把三类资产拆开并定安装权限。

## 要求 / 规则
- 三类资产：
  - **A 纯声明包**（Org/Role/ProcessDef/Authority/Schedule/KPI/路由）：
    可由 root 提议，可由 Owner 发布；
  - **B 受信能力包**（Tool handler/Integration adapter/MCP adapter/
    Skill script/external executor）：**仅 Provider 安装**，需签名 +
    信任声明；
  - **C 数据包**（KB seed/模板/词典/示例数据/资产引用）：可导入，但
    内容默认**不可信数据**。
- 包 schema 必备：`package_id / version / api_compatibility /
  content_hash / signer / dependencies / capabilities_requested /
  namespace`。
- 命名冲突处理（显式规则，不能静默覆盖）：工具名 / role 名 / record
  schema / Skill 覆盖 / 同一 Integration 被多包声明 / 包卸载后实例继续
  运行 / 包依赖工具被撤销。
- Skill 信任：`scripts/` 单独视为能力包，不属于 Skill；Skill 默认只
  引用已有 Tool；Skill 不能自行提升权限；Skill 的 prompt/KB/外部
  payload 必须带来源与信任等级。
- 提示词注入隔离（落到 ContextCompiler）：不可信客户消息/评论/邮件不得
  直接作为系统指令注入 briefing；用结构化来源段
  `[UNTRUSTED_CUSTOMER_CONTENT] / [SKILL_INSTRUCTION] / [POLICY]`，
  并规定不同来源优先级。

## 产出
- 三类资产边界 + 安装权限 spec。
- 包 schema（签名/依赖/命名空间）与冲突处理规则。
- Skill 信任来源 + 提示词注入隔离规则（供 ContextCompiler 实现）。

## 验收标准
- [ ] 配置包不能携带未声明/未签名/未授权的可执行能力（加载即拒绝）
- [ ] Skill 的 prompt/KB/外部 payload 带来源与信任等级
- [ ] 不可信客户内容不当作系统指令注入
- [ ] 工具名/role 名/record schema 冲突显式报错，不静默覆盖
- [ ] 最小测试向量段：非法包被整体拒绝（不半装）通过
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
