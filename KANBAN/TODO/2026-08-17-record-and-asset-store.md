# v0.10-10: RecordStore 与 AssetStore

**Phase:** v0.10 边界
**Source:** SPEC §7.3、§7.4；OI-005 §1.4、OI-006 §3
**Priority:** high

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
