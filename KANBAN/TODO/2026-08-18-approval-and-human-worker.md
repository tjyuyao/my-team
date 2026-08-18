---
kind: task
phase: v0.10 人类参与
source: SPEC §10、§7.5；OI-005 §3、OI-006 §3
priority: high
---

# v0.10-12: ApprovalGate、Human Worker 与 CredentialStore


> ⚠️ **须重划（勿按本卡正文直接开工）**：`HUMAN_APPROVAL` pending op 模型
> 已被 `KANBAN/PLAN/v0.10.0-plan` 判为过时——须统一为 HumanTask
> （work/approval/decision/consultation），按三查分离（Capability /
> Authority / Gate）设计，human 身份须经认证（Identity 闭包），升级改用
> 结构化 escalation（on/mode/target）。完整重写待 SPEC §10.2 对齐后进行
> （见 plan「待重划项」）；是否拆为 ApprovalGate / Human Worker /
> CredentialStore 三卡亦在重划时决定。下文过时条目已标注。

## 目标
高风险操作需要真实的人类审批；人类可以作为组织树中的 Worker
接受委派与任务；平台/LLM 凭证引用式存储。

## 要求 / 规则
- `requires_approval` 或审批策略触发时，Act/Validate 生成
  `HUMAN_APPROVAL` pending op，而不是直接拒绝。
  **〔待重划：统一为 HumanTask 模型〕**
- 审批 UI/API：approve/reject + 附言；批准后由 Publish 继续执行；
  拒绝则 op 取消并唤醒 Agent。 **〔待重划：按三查分离重设计〕**
- 审批有 deadline 与升级；审计记录谁在什么上下文批的。
  **〔待重划：升级改用结构化 escalation〕**
- `AgentConfig.kind="human"`：Human Worker 有任务队列，Manager
  可委派；人类通过 UI accept/complete/fail，翻译为 Intent 走
  相同事务路径。 **〔待重划：human 身份须经认证（Identity 闭包）〕**
- `CredentialStore`：引用式 `credential_ref` → 外部 KMS/env/加密
  文件；密钥不进 Journal/审计/prompt/DB 明文。

## 产出
- ApprovalGate 生命周期与 API。
- Human Worker 最小闭环（Manager 委派 → 人完成）。
- CredentialStore 引用接口。

## 验收标准
- [ ] 高风险工具在审批通过前不执行
- [ ] 审批拒绝后 op 取消且 Agent 被唤醒告知
- [ ] 人类 Worker 可被 Manager 委派并完成任务
- [ ] 密钥明文不出现在 Journal/审计/DB 中
- [ ] 新测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过
