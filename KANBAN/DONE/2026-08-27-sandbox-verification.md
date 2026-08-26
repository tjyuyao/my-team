---
kind: task
status: completed
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

## 完成

- 故事测试 `tmp/check_sandbox_verification.py` 补 5 个 gap：pidns 防杀
  兄弟（宿主受害者不可见）、ipcns 隔离（SysV shmget ENOENT）、per-position
  挂载矩阵一致（无 per-position 物化）、伪造 source 读侧盖章覆盖、重装
  同身份旧通道不残留（旧 reader EOF 退出 + 新 socketpair 生效）；其余
  验收点由 check_mount / check_network / check_bash_sandbox 覆盖。
- 审查：PASS-with-nits（主 agent + 独立 subagent 双审）；修补随收尾
  （pidns 受害者 PID>10 防撞号、ipcns errno 收紧为 ENOENT(2)、per-position
  打印不探 Authority 内部、PID 打印改信息性）。
- 全量回归（demo + 全部 check_*.py）PASS；文档一致性收口（SPEC /
  PROTOCOL / AUTHORITY.md §8 补覆盖条目，无过期表述）；lint 0。
