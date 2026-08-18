---
kind: task
status: completed
phase: 5 - Reliability
source: "review #19; report §7 P2"
priority: medium
---

# DeterministicReplay Scoping


## 目标

明确 `DeterministicReplay` 的保证范围和限制。

## 背景

报告声称"同一输入产生同一输出"，但实际上无法保证：

- 跨进程回放
- LLM 输出回放
- 文件系统状态一致性
- 线程调度确定性

## 要求

1. 更新 `reliability.py` 文档注释，明确限定范围
2. 更新报告中的回放保证声明
3. 记录需要保存的状态：initial state, tick duration, mail schedule, observations, action plans, tool results, random seeds, commit decisions

## 产出

- [ ] 更新 `reliability.py` 的 `DeterministicReplay` 文档
- [ ] 更新报告中的回放声明

## 验收标准

- [ ] 文档明确列出回放的前置条件和限制
- [ ] 不再声称系统整体确定性
