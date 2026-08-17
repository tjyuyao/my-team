# v0.10-12: ApprovalGate、Human Worker 与 CredentialStore

**Phase:** v0.10 人类参与
**Source:** SPEC §10、§7.5；OI-005 §3、OI-006 §3
**Priority:** high

## 目标
高风险操作需要真实的人类审批；人类可以作为组织树中的 Worker
接受委派与任务；平台/LLM 凭证引用式存储。

## 要求 / 规则
- `requires_approval` 或审批策略触发时，Act/Validate 生成
  `HUMAN_APPROVAL` pending op，而不是直接拒绝。
- 审批 UI/API：approve/reject + 附言；批准后由 Publish 继续执行；
  拒绝则 op 取消并唤醒 Agent。
- 审批有 deadline 与升级；审计记录谁在什么上下文批的。
- `AgentConfig.kind="human"`：Human Worker 有任务队列，Manager
  可委派；人类通过 UI accept/complete/fail，翻译为 Intent 走
  相同事务路径。
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
