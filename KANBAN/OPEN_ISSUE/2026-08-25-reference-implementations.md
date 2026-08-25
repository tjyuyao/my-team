---
kind: issue
status: open
source: 用户指定（2026-08-25）；N4 记忆与注入 / Agent 架构对照
priority: medium
---

# 参考实现：PENpi + tau（本地克隆，设计对照时引用）

**Opened:** 2026-08-25
**Status:** OPEN — 参考实现已入库，对照点待设计评审时拉取

## 目的

引入两个外部项目的本地克隆作为**参考实现**，在设计/评审 my-team 的
记忆系统（N4）与 Agent 架构时对照引用。只读参考，不合并代码、不引入
依赖。

## 参考实现清单

| 项目 | URL | 本地路径（tmp/，gitignore） | 克隆 | 定位 |
|---|---|---|---|---|
| PENpi | https://github.com/penfieldlabs/PENpi | `tmp/PENpi` | 2026-08-25（--depth 1，27M） | Pi 的 fork：给编码 agent **持久记忆**——FIFO 上下文管理（watermark 修剪）替代惯例压缩 + Penfield 知识图谱（`recall`/`store`/`connect`/`explore`/`reflect`），跨会话存活 |
| tau | https://github.com/huggingface/tau | `tmp/tau` | 2026-08-25（--depth 1，8.2M） | HuggingFace 小型可读终端编码 agent——"如何构建编码 agent 的工作示例"（读文件/工具调用/上下文管理） |

## 已知对照点（设计评审时拉取，非结论）

- **PENpi ↔ N4 记忆**：PENpi 三 tier（上下文窗口 FIFO / Penfield 图谱 / …
  另一 tier）vs my-team 工作记忆（注入集 = 固定 ∪ 触发 ∪ 召回 ∩ 预算）
  + 整理模式 CONSOLIDATING——对照点：watermark 修剪 vs 预算超限触发；
  知识图谱 vs 记忆条目 + associated 关系；`reflect` 型主动整理 vs
  memory_fold/promote。
- **tau ↔ Agent 架构**：tau 的工具调用/上下文管理/可读性设计 vs
  my-team Agent 引擎（工具面 ToolPlugin API、注入组装、continuation）——
  对照点：单 agent 终端形态 vs 多 agent 异步 tick 形态的差异是否带来
  可借鉴的工具契约设计。

## 待办

- [ ] N4 设计评审时对照 PENpi 记忆架构（FIFO/图谱/整理动作），确认或
      修正 N4 设计决策（预算机制/条目结构/整理工具集）；
- [ ] Agent 工具面/上下文评审时对照 tau（如需）；
- [ ] 对照结论写回对应卡/设计文档；参考实现不进入交付物。

## 备注

- 浅克隆（--depth 1），如需全量历史/子模块另行拉取；
- tmp/ 已被 .gitignore 排除，克隆不入版本库；本卡是唯一的持久记录。
