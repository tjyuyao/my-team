---
kind: task
status: completed
phase: v0.10 边界
source: SPEC §7.3、§7.4；OI-005 §1.4、OI-006 §3
priority: high
---

# v0.10-10: RecordStore 与 AssetStore


## 目标
提供类型化结构化记录存储（含不变量与 ledger 投影）与二进制资产
存储，支撑电商库存/订单、自媒体内容资产、知识星球会员等场景。

## 要求 / 规则
- `RecordStore`：
  - 记录类型由场景包 schema 注册；注册即校验。
  - 提供 `RECORD_UPSERT` 与 `RECORD_DELTA` 两类 effect；
  - CommitValidate 检查记录级不变量（库存非负、单号唯一、金额
    合法、到期日合法）；
  - append-only ledger 投影当前状态；审计/对账从 ledger 推导。
- `AssetStore`：
  - 内容寻址（sha256）；put/get/stat；
  - 私有文件快照支持二进制读取（不再跳过二进制）；
  - Email 附件与内容资产引用 AssetStore 对象。
- 先实现内存版，随后接入 SQLite 持久化与统一 Journal。

## 产出
- RecordStore/AssetStore 模型与 effect 集成。
- 库存/订单最小闭环测试（采购入库与销售出库同 tick 原子）。

## 验收标准
- [ ] 库存扣减到负数被 CommitValidate 拒绝
- [ ] 订单与库存变更同 tick 原子提交/回滚
- [ ] ledger 可重放并推导当前库存
- [ ] 二进制文件可写入、读取、作为附件引用
- [ ] 新测试；`uv run pytest -q` 全绿；`ruff`/`mypy` 通过

## 完成注记（2026-08-18）

实现要点：
- `record_store.py`（新）：`RecordStore` + `RecordSchema`（字段/不变量/唯一
  字段，注册即校验）+ `LedgerEntry`（append-only 变更来源）+ `MutationResult`。
  - `upsert` / `apply_delta`：所有变更走 effect；不变量（非负/正/唯一/必填）
    在 store 内检查，违反抛 `RecordInvariantError` → commit 路径当可判定业务
    失败（T18 局部 FAILED，绝不整回合回滚）。
  - `replay()` 从 ledger 重放推导当前状态（库存对账推导）。
  - `invert_mutation(record_type, key, prior, ledger_ids)`：逐 effect 撤销
    （重载 prior + 裁剪 ledger 条目）——T18 逆操作在读录侧落地。
- `asset_store.py`（新）：`AssetStore` 内容寻址（sha256）put/get/stat，
  同内容自动去重；`AttachmentRef`（ref_type/path/version/hash/size/mime，
  SPEC §4.3 大内容只存引用）——Email 附件据此引用（T8b 接邮件侧）。
- `transaction.py`：新增 `RECORD_UPSERT` / `RECORD_DELTA` effect 类型 + invert
  契约（RESTORE_PREVIOUS：prior record + ledger_ids）。
- `simulation.py`：`_record_store` / `_asset_store` 子系统 + public 属性 +
  插件 handles；`record_upsert` / `record_delta` 两个 STAGED_MUTATION 工具；
  apply 分支捕获 RecordInvariantError → `_fail_locally`；invert 分支恢复记录；
  FILE_WRITE 支持 `content_bytes_b64` + `is_binary`（二进制私有读写不再跳过）；
  `_read_private_file_bytes` 内核二进制读通道；_collect_state/load 持久化两个
  store（含 ledger/records/schemas/asset blobs）。
- `tool_manifest.py`：`record_upsert` / `record_delta` manifest；内置工具
  15 → 17。
- 测试 `tests/test_record_asset_store.py`（22 个）：schema 注册/重复拒、负库存
  拒、唯一字段/必填拒、ledger 重放、invert 还原+裁剪、Asset put/get/stat/去重/
  AttachmentRef、commit 集成（upsert+delta 同 commit、负库存局部 FAILED 其余照
  常提交、采购入库+销售出库同 tick 原子、超卖同 tick 拒绝、不变量不升格内核回
  滚、全回滚还原记录、schema 未注册 tool 拒绝）、私有二进制写读+回滚 byte 精确。
- 全量 842 passed（820+22）；mypy clean（45 源文件）；ruff 通过；kanban_lint 0。
- 范围确认：内存版先行，SQLite 持久化/统一 Journal 属后续（卡已列"先内存"）；不
  动邮件/上下文系统（附件引用留 T8b）。

## 验收核对
- [x] 库存扣减到负数被 CommitValidate 拒绝（RecordInvariantError → 局部 FAILED）
- [x] 订单与库存变更同 tick 原子提交/回滚
- [x] ledger 可重放并推导当前库存
- [x] 二进制文件可写入、读取、作为附件引用（AssetStore + AttachmentRef）
- [x] 新测试；`uv run pytest -q` 842 passed；`ruff`/`mypy` 通过
