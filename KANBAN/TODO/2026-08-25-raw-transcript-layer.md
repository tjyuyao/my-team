---
kind: task
phase: v0.11 agent-impl
source: SPEC §4.5（PrivateStore 原始层）；grill（2026-08-25）
priority: medium
---

# 原始对话记录层（Agent 私有原始层）

## 目标

Agent 的私有记忆分两层：条目网络是「精炼层」（N4 管），原始对话是
「原始层」（本卡管）。本卡做原始层：prompt/response 全文 append-only
落 PrivateStore，可全文检索，与条目层的召回路径分开。

## 做什么（大白话）

1. **忠实记录**：每次 LLM 调用后，prompt 全文 + response 全文
   append-only 写到 agent 的 PrivateStore（conversation JSONL），不被
   摘要/折叠破坏——这是「当时它到底想了什么」的完整原始凭证。
2. **全文检索**：提供检索工具，按时间 / task_id 流式返回原始消息片段
   （像搜聊天记录），恢复被精炼层丢弃的细节。
3. **两条检索路径分开**：条目层走触发器召回（关键词/语义匹配 memory_points），
   原始层走全文检索（时间/任务流式）——「我想要相关知识」和「我想知道
   当时发生了什么」是两种不同的查询，不混用一个引擎。
4. **体积**：定期归档（Owner 审批）解决，不设自动删除；过早的数据可归档
   或删除（Owner 审批）。

## 产出

- PrivateStore 下的 conversation JSONL 写入（append-only）；
- 全文检索工具（按时间/task_id 流式）；
- 与条目层召回路径的区分（测试证明不混用）。

## 验收标准

- [ ] prompt/response 全文 append-only 落盘，可全文检索
- [ ] 条目层召回与原始层全文检索路径区分（不混）
- [ ] 无自动删除；归档入口存在（Owner 审批）
- [ ] `uv run pytest -q` 全绿；ruff/mypy 通过

## 依赖

- N4-1 记忆模型（条目层 `associated` 关联原始记录定位）
- 与 N4 联测：精炼层（条目）+ 原始层（对话）共同构成 agent 私有态两层
