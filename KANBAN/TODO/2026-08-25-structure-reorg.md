---
kind: task
phase: v0.11 agent-impl
source: 架构讨论（2026-08-25）；docs/V011_CODEBASE_MIGRATION.md
priority: high
---

# 代码结构重排（四桶 + 重命名，N4 落地后立即执行）

> **2026-08-25 提前（Owner 后悔放最后）**：原定 v0.11 收尾执行，
> 现改为 **N4 落地后、N5 之前**执行——避免 N5 在旧扁平结构上继续
> 写代码累积债务（47 个扁平 .py，只有 devices/models 两个子目录，
> 三态未落地）。N1c 已完成、N4 收尾、N5 未动 = 最佳窗口。

## 目标

N1c + N4 落地后，做一次**机械性**目录重排 + 重命名，让
src/my_team 的结构反映三态（内核/设备/Agent）+ 模型/契约族，
并让 N5 及之后的代码直接写在正确结构里。

## 已定决策（2026-08-25）

1. **单数目录名**：`device/`、`agent/`（不是 devices/agents）。
2. **四桶**：`kernel/`（纯逻辑）+ `device/`（数据+工具+ACL）+
   `agent/`（引擎）+ `models/`+`protocols/`（数据模型与契约）。
3. **命名修正**（重排时执行）：
   - `file_ops.py` → `models/`（它是审计数据模型，不是文件操作逻辑）
   - `shared_kb.py`/`mailbox.py`/`record_store.py`/`asset_store.py`/
     `credential_store.py`/`task_tree.py` → `kb.py`/`mail.py`/
     `records.py`/`assets.py`/`credentials.py`/`tasks.py`
   - `agent_runtime.py` → `contract.py`（它是协议/接口）
   - `context_compiler.py` → `injection.py`（N4 重写时直接起新名）
   - **`journal.py` 不改名**（2026-08-25 定：不做世界记忆设备接口层，
     恢复/重放机制裁撤，Journal 保持 append-only 记录现状；改名
     会名不副实）
4. **N4 记忆代码归位**（本次新增）：`memory_store.py`/`memory_recall.py`
   → `agent/`（记忆是 Agent 引擎数据面，非设备、非内核）。

## 交付

- 一次机械 commit（目录 + import 更新 + 改名），全量测试验证；
- 结构对齐扩展接缝卡（`extension-seam`）的包结构命名；
- N5 之后的代码按新结构落盘（不再回旧扁平结构）。

## 验收标准

- [ ] 目录反映四桶；命名反映内容（无「名不副实」残留）
- [ ] 全量测试绿；`ruff`/`mypy` 干净
- [ ] 纯机械（无行为改动），一次 commit 可审

## 依赖

- N1c + N4 落地（文件最终形态定了）
- 在 N5 之前执行（N5 起新代码直接写进新结构）
