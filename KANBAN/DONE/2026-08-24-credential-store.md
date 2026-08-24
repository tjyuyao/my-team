---
kind: task
status: completed
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

## 难点 / 风险注记（2026-08-19，分析成果固化）
- **范围封闭，中等偏易**：本质是补 T9 的 `credential_ref` 落地——
  Integration 字段已定义（integration.py），store 未实现。
- **断言可枚举**：密钥不出 Journal/审计/DB/prompt 的可观测面有限
  （_collect_state / journal / audit / ContextCompiler 均可枚举），测试断言
  好写；凭证解析在工具执行层（out-of-process executor / plugin 侧），内核
  本来看不到明文。

## 验收标准
- [x] `credential_ref` 可解析为可用凭证；无引用时明确报错
- [x] 密钥明文不出现在 Journal/审计/DB/prompt 中（测试断言）
- [x] `uv run pytest -q` 全绿（924 passed）；`ruff`/`mypy` 通过

## 实现注记（2026-08-24，T12b 完成）
- **新增 `src/my_team/credential_store.py`**：`CredentialStore`（ref
  路由 `kind:name` → 后端）+ 两个后端：
  - `EnvCredentialBackend`（`env:VAR`，读环境变量，值不落任何状态）；
  - `EncryptedFileCredentialBackend`（`file:ENTRY`，stdlib-only：
    scrypt 派生密钥 + HMAC-SHA256 计数器流加密，落盘无明文、原子写；
    模拟器级加密，生产应指向真实 KMS）。
  - 错误类型：`MissingCredentialRefError`（无引用明确报错）、
    `CredentialNotFoundError`（引用不存在）、`CredentialDecryptError`。
  - `resolve()` = executor/plugin 边界取值（不落可观测面）；
    `has()` = value-free 门禁；`snapshot()` 只暴露条目名，永不暴露值。
- **内核接线（simulation.py）**：`Simulation.credential_store` +
  `set_credential_store()`；plugin handles 注入 `credential_store`；
  Phase 9 dispatch 对声明了 `credential_ref` 的出站工具加门禁——ref
  不可解析 → 永久拒绝（audit `credential_unresolvable`，op 失败），
  不背压、不泄露值。空引用跳过门禁（既有行为不变）。
- **测试 `tests/test_credential_store.py`（19 个）**：单元（env/加密
  文件/错误/快照只含名不含值）+ 内核集成（出站工具经 ref 取凭证、
  假平台 out-of-band 完成 op；`_observable_text` 枚举 Journal/审计/
  DB 组件快照/ContextCompiler prompt + SQLite 文件字节，断言密钥明文
  不出现；不可解析 ref 永久拒绝审计可追溯）。测试全用假凭证。
- **SPEC**：§7.5 / §12.4 加"已实现"小注记。
- 全量验证：`uv run pytest -q` 924 passed（基线 905 + 新 19）；
  `ruff check src tests` 与 `mypy src` 均通过；`kanban_lint` 0 violation。
