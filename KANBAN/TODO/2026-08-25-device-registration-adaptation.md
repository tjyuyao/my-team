---
kind: task
phase: v0.11 agent-impl
source: SPEC §5.2–5.11；三态收敛（2026-08-24）；拆分自原 device-model（N1 → N1a/N1b/N1c）
priority: high
---

# N1c 设备归位（存量适配）


## 目标

现有 store 归位为设备：注册受控 uuid + 注入内容声明；simulation 的
工具处理器（~830 行）按设备域拆出。**依赖 N1a**；Task 设备细粒度
position 求值随 N5（依赖 N2）。

## 要求 / 规则

- **基础设备注册适配**：SharedKB / RecordStore / AssetStore /
  CredentialStore / Mailbox——各自注册受控 uuid（条目/范围、工具/
  工具包）+ 声明注入 content（§5.1 三条：不维护账本 / 身份落字段 /
  注册即声明注入内容）；
- **工具处理器拆域**：simulation `_register_tool_handlers`（~830 行）
  按设备域拆出，经 ToolPlugin API 注册（§5.1：设备注册 = 向 Authority
  注册工具 uuid）；
- **预算拆分**：LLM API 限额归 Agent 引擎（§4.6，N4 侧）；外部资源
  限额与 Ingress/Integration 设备一起管理（§5.11）；
- **executor 平台级 Admission**：rate_limit / 健康背压归 Integration
  设备（§3.4/§5.11）；
- **日历/调度数据归设备**：CronSpec / ScheduleRule 数据面（算法留
  内核 Schedule 阶段）；容量参数已在 N1a 配置设备；
- **Task 设备公共数据层**：任务树归 Task 设备（细粒度按 position
  求值随 N5，依赖 N2）。

> **注（2026-08-25）**：世界记忆设备接口层（Journal 持久化/查询归
> 设备）**不再做**——Journal 保持 simulation 层现状（append-only
> 记录 + 状态快照持久化），与恢复/重放机制一起裁撤（见 DONE
> pending-outbox-recovery）。Journal 设备化延后到需要查询消费方时
> 再议。

## 产出

- 各设备注册适配 + 注入内容声明；
- 工具处理器按域拆出（simulation 瘦身）；
- 预算 / Admission / 日历数据归位。

## 验收标准

- [ ] 各设备经接口注册受控 uuid 与注入 content（有测试）
- [ ] 预算拆分生效（LLM 限额 Agent 内、外部速率 Ingress）
- [ ] 设备依赖经接口声明（无跨设备直连）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过

## 设计定稿（2026-08-25）

详细设计见 `docs/N1C_DEVICE_REFIT_DESIGN.md`。关键决策：
- **设备归位 = 改造 store 为 Device 子类**（否决适配器：注入声明必须附着
  数据所在处、序列化一次解决）；capability 必须 **adopt uuid5 派生值**（禁
  随机 uuid4，防 manifest_hash 断裂）；
- 私密区文件工具（read/ls/write/apply_patch）与执行器族（run_tests/
  python_*/git_*）**非设备**（§4.5/§3.4），仅搬移归属；
- Task 细粒度 position 求值留 N5；预算/容量归位**值不变只换归属**；
- RecordStore 删 ledger：回滚改 invert_data 前值机制（§3.3）。**（2026-08-25 暂缓：删 ledger 属 Journal 投影层，见 OPEN_ISSUE journal-projections；RecordStore 现持当前状态即可，回滚维持 invert_data + ledger_ids 现状）**

**子任务**：N1c-1 设备适配层 → N1c-2 工具拆域（独占 simulation）→
{N1c-3 预算+Admission+日历} → N1c-4 Task 公共数据层。每步全量回归
再放行。（2026-08-25：原 N1c-3 世界记忆设备接口层裁撤，原 N1c-4/5
顺延为 N1c-3/4）
