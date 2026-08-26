---
kind: task
phase: v0.14
source: SPEC.md
priority: high
---

# 沙箱与出站通道验证 + 全量回归

## 内容

- 沙箱故事测试：写不进他人数据区、禁网拒绝、userns/pidns 防杀兄弟、
  **System V IPC 隔离（ipcns，沙箱内 semget/shmget 与宿主不互通）**、
  bash 命令落数据区、needs_network 声明生效、**不同 position 设备挂载
  矩阵一致（无 per-position 物化）**；
- 出站通道故事（B 语义）：伪造 source 被宿主 reader 读侧盖章覆盖；
  重装同身份后旧连接不残留（terminate → reader EOF 退出，新身份新
  socketpair 生效）；
- 全量回归（tmp/check_*.py + demo）PASS；
- 文档与代码一致性收口（提交前；落文档不入卡）。

## 验收

全部通过；文档与代码一致。

## 依赖

data-dir-convention、sandbox-wrapper（含 outbound-channel 并入内容）、
network-declaration、bash-sandbox-adapt（全部已提交后）。
