---
kind: task
phase: v0.11 post-agent
source: 架构讨论（2026-08-25）；docs/V011_CODEBASE_MIGRATION.md
priority: medium
---

# 代码结构重排（四桶 + 重命名，重构潮后执行）

## 目标

N1c-2/3/4/5 + N4 落地后，做一次**机械性**目录重排 + 重命名，让
src/my_team 的结构反映三态（内核/设备/Agent）+ 模型/契约族。

## 已定决策（2026-08-25）

1. **单数目录名**：`device/`、`agent/`（不是 devices/agents）。
2. **四桶**：`kernel/`（纯逻辑）+ `device/`（数据+工具+ACL）+
   `agent/`（引擎）+ `models/`+`protocols/`（数据模型与契约）。
3. **命名修正**（重排时执行）：
   - `file_ops.py` → `models/`（它是审计数据模型，不是文件操作逻辑）
   - `journal.py` → `world_memory.py`（世界记忆设备，对齐 SPEC §5.9）
   - `shared_kb.py`/`mailbox.py`/`record_store.py`/`asset_store.py`/
     `credential_store.py`/`task_tree.py` → `kb.py`/`mail.py`/
     `records.py`/`assets.py`/`credentials.py`/`tasks.py`
   - `agent_runtime.py` → `contract.py`（它是协议/接口）
   - `context_compiler.py` → `injection.py`（N4 重写时直接起新名）
4. **现在不动**：约 79% 文件在途（会被 N1c/N4/N5/N6 大动），现在挪 =
   文件被碰两次，且改名会跟 N4/N5 的重写打架。

## 交付

- 一次机械 commit（目录 + import 更新 + 改名），全量测试验证；
- 结构对齐扩展接缝卡（`extension-seam`）的包结构命名。

## 验收标准

- [ ] 目录反映四桶；命名反映内容（无「名不副实」残留）
- [ ] 全量测试绿；`ruff`/`mypy` 干净
- [ ] 纯机械（无行为改动），一次 commit 可审

## 依赖

- N1c-2/3/4/5 + N4 落地（文件最终形态定了）
- 本卡应是 v0.11 收尾步骤（在 post-agent 末尾）
