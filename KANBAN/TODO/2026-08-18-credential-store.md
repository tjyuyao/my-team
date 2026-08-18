---
kind: task
phase: v0.10 边界
source: SPEC §7.5；§12.4
priority: medium
---

# v0.10-12b: CredentialStore（引用式凭证存储）

## 目标
平台/LLM 凭证引用式存储：内核与业务代码只见 `credential_ref`，密钥
实体不进入内核任何可观测面。

## 要求 / 规则
- `CredentialStore`：引用式 `credential_ref` → 外部 KMS / env / 加密
  文件解析。
- 密钥不进 DB、不进 Journal、不进审计、不进 prompt（SPEC §12.4）。
- Integration / 出站工具通过 `credential_ref` 取用，不持明文。

## 产出
- CredentialStore 引用接口（resolve）+ 至少一种后端（env/加密文件）。

## 验收标准
- [ ] `credential_ref` 可解析为可用凭证；无引用时明确报错
- [ ] 密钥明文不出现在 Journal/审计/DB/prompt 中（测试断言）
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
