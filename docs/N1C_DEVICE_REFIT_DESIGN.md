# N1c 设备归位 — 设计定稿与任务拆解

> 2026-08-25 定稿。设计草案由设计 agent 产出，主 agent 审阅定案。
> 对应卡：`KANBAN/TODO/2026-08-24-device-registration-adaptation.md`。
> 目标：现有 store 归位为设备 + simulation 工具处理器拆域 + 世界记忆
> 设备接口 + 预算/Admission/日历数据归位 + Task 设备公共数据层。

## 1. 设备归位方式（定案：改造 store 为 Device 子类）

每个 store 类（SharedKB/RecordStore/AssetStore/CredentialStore/
MailSystem/TaskTree）直接继承 `Device`：simulation 持有的实例不变
（同一对象既是 store 又是 device）；工具 handler 从 simulation 移入
设备模块成为设备方法；设备构造时注入窄内核服务（TransactionBuffer/
AuditLog/LockManager 等，SharedKB 已有先例）；simulation 构造区只做
组装（register_to + register_tool）。

否决适配器方案（store 不动、外层包装）理由：§5.1 定义设备=数据+工具，
一体化是字面表达；注入声明必须附着在数据所在处（页面级/账号级信息只
有 store 自己知道）；`_collect_state` 清理为设备自持 snapshot/restore
是绕不开的公共成本，A 一次到位。

**关键实现约束（最高优先）**：内置工具 capability 必须**沿用契约派生的
uuid5**（`ToolManifest._derive_capability`），设备用 **adopt**（`Device.
register_entity` 支持显式 entity_id）而非新生成 uuid4——否则
manifest_hash 每次运行变化，重放/审计链断裂（test_tool_protocol 哨兵）。

## 2. 工具处理器拆域清单（17 个内置工具）

| 域 | 工具 | 归属 |
|---|---|---|
| 私密区文件（**非设备**，§4.5） | read/ls/write/apply_patch | Agent 引擎侧工具模块；`device_id=""`，capability 沿用 uuid5；授权路径不变 |
| 执行器/工作区（**非设备**，§3.4） | run_tests/python_compute/python_transform/git_diff/git_status | 内核执行面（执行器族+沙箱）；仅搬移+归属声明 |
| 知识库设备（§5.2） | kb_write/kb_read/kb_list/kb_search | SharedKBDevice；有 InjectionDecl（`[KB_INSTRUCTION]`，N4 消费） |
| 记录设备（§5.3） | record_upsert/record_delta | RecordDevice；记录族范围 DATA 实体 |
| 邮箱设备（§5.6） | send_email | MailDevice；mail 范围 DATA 实体 |
| Task 设备（§5.7） | delegate（TASK_CREATE+EMAIL_SEND 组原子） | TaskDevice；EMAIL_SEND 是 effect 声明非直连 MailDevice（无跨设备直连） |
| 资产设备（§5.4）/凭证设备（§5.5） | 无 agent 工具 | 注册设备 + DATA 实体；工具面预留 |
| 世界记忆设备（§5.9）/Ingress（§5.11） | 无 agent 工具 | 注册设备 + DATA 实体 |
| Integration 设备（§5.11） | 外部 EXTERNAL 工具 | IntegrationDevice 持有 rate_limits/health；admit 变设备接口 |
| 日历设备（§7.1） | 无 agent 工具 | CalendarDevice（数据面）；算法留内核 |

## 3. 世界记忆设备接口（§5.9）

`WorldMemoryDevice(Device)` 持有 TickJournal 实例（组合）：
- **写入/回滚逻辑留内核**（TickJournal.start_tick/finalize/AuditLog 写入不动，§3.2）；
- 设备接口：`append(record)` / `query`/`last`/`for_tick`（只读）/ `replay()`
  （重放入口）/ `audit_projection`/`kpi_projection` / `snapshot`/`restore`；
- `attach_backend(backend)`：`PersistenceBackend` protocol —— N1c 实现
  `MemoryBackend`（默认），**N6 实现 SQLiteBackend**（保存/恢复从 Journal 重放）；
- 分界判定（验收"内核无数据直连"）：内核只做写入逻辑；**一切读**经设备
  接口；`_collect_state/_restore_state` 不得摸 `_journal._records`（过渡：
  入口换 `world_memory.snapshot()/restore()`，格式不变保 test_snapshot_matrix）；
- 顺带接线：`OrgStructure.journal_sink` → 世界记忆设备（组织调整入 Journal）。

## 4. Task 设备公共数据层

`TaskTree` → `TaskDevice(Device)`：数据与 CRUD 原样保留 + 范围级 DATA
实体 + delegate TOOL 实体 + InjectionDecl；delegate handler 归位
（组原子，§2）。**细粒度 position 求值留 N5**：任务动态高频创建需
"写路径注册钩子 + 授予时机语义"，且可见性由岗位边语义定义——那是 N5
任务治理绑定（边语义校验 + Authority 裁决）的核心，N1c 提前做会范围
失控；Authority 已支持任意 entity_id 授予，N5 补数据即可。

## 5. 预算 / Admission / 日历归位

- **预算拆分**：LLM API 限额（数据面）→ ConfigDevice（Agent 引擎侧，
  §4.6）；外部资源限额 → IntegrationDevice（§5.11）；PreValidate 判定
  留内核；
- **executor 平台级 Admission** → IntegrationDevice：`admit()/
  record_dispatched/health` 变设备接口，`放行 := executor ∧ provider`
  不变；health_check 数据面归位；
- **日历数据** → CalendarDevice（CronSpec/ScheduleRule 注册/advance 数据
  变更）；到期判定/RULE_ADVANCE 留内核；AgentScheduler（唤醒/激活/SLA
  排序）是调度内核+运行时状态，不归设备；
- **默认值漂移风险（必须）**：`CapacityLimits`（4/16/64）与现状
  `ExecutionConfig`（1/8/32）不一致——迁移必须"值不变只换归属"
  （ConfigDevice 由现有 ExecutionConfig 填充），否则 PreValidate 判定
  变化波及 test_budget/test_sla_capacity。

## 6. 任务拆解

| 子任务 | 范围 | 验收要点 | 依赖 | 并行 |
|---|---|---|---|---|
| N1c-1 设备适配层 | store 子类化 Device + 注册受控 uuid/InjectionDecl + adopt_entity 机制 | 每设备注册 + 注入 content（测试）；存量 store 测试全绿 | N1a | 前置单独 |
| N1c-2 工具拆域（分批） | 17 handler 按 §2 移入设备/引擎模块；`_register_tool_handlers` 瘦身 | 工具行为不变（全量绿）；manifest device_id/capability 绑定正确 | N1c-1 | **独占 simulation 该区域** |
| N1c-3 世界记忆设备接口 | WorldMemoryDevice + collect/restore 入口替换 + org.journal_sink | 内核无数据直连；snapshot matrix 绿 | N1c-1（弱） | 与 N1c-4 并行 |
| N1c-4 预算+Admission+日历数据 | 容量字段归位（值不变）+ IntegrationDevice + CalendarDevice | 预算拆分生效；日历数据在设备；test_budget/sla_capacity/calendar 绿 | N1c-1（弱） | 与 N1c-3 并行；与 N1c-2 错开构造区 |
| N1c-5 Task 公共数据层 + Record ledger 删除 | TaskDevice + delegate 归位；RecordStore 删 ledger（回滚改 invert_data 前值机制） | 无 ledger 字段；回滚语义不变（test_commit_rollback 绿） | N1c-1/2 | 建议在 3/4 之后 |

**simulation.py 共享文件分区**：构造区（1/4）、`_register_tool_handlers`
（2 独占）、`_collect_state/_restore_state`（3/5）、`_phase_commit`（5）、
`_phase_dispatch`（4）。推荐顺序：N1c-1 → N1c-2 → {N1c-3 ‖ N1c-4} →
N1c-5；每步全量回归再放行。

## 7. 风险清单（要点）

1. **manifest_hash 稳定性**（最高）：capability 必须 adopt uuid5，禁随机 uuid4；
2. **ledger 删除回滚等价**：RecordStore 回滚从"ledger 反查"改"apply 时把
   前值写入 StagedEffect.invert_data"（§3.3 逆操作，与 FILE_WRITE 同构）；
   `_version_counter` 状态化；test_commit_rollback/snapshot matrix 哨兵；
3. **默认值漂移**：归位不改变行为；
4. **双轨权限并存期**：kb 工具仍走 PermissionEngine（路径）+ Authority
   （能力）双门，求值顺序明确并写测试；
5. `_restore_state` 防御式读取（旧存档无新 key，沿用 get() 先例）；
6. 存量测试波及清单见设计草案 §7.1（test_record_asset_store 直接改
   断言；snapshot/persistence/tool_manifest/budget 族随迁）。

## 8. 衔接点

- **N4**：各设备 InjectionDecl.source_tag 对齐来源段标签；injection_for
  钩子已就绪，N1c 填好声明即被 N4 消费；
- **N5**：Task 细粒度求值 + delegate 边语义校验 + 旧 authority.py（8 域
  裁决）集成——N1c 只铺数据/接口面；
- **N6**：PersistenceBackend SQLite + `_collect_state` blob 换 replay()；
- **T13**：设备注册 → org 初始化 register_to + ConfigDevice.apply_to
  （引导路径已存在）。
