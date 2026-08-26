---
kind: task
phase: v0.14
source: SPEC.md
priority: medium
---

# needs_network 显式声明机制

## 内容

默认禁网；需要网络的进程显式声明（进程级资源，非权限 scope）：
- 声明通道：设备经 options（安装 payload options.needs_network）→
  load_spec → 沙箱参数（不 `--unshare-net`）；agent 经 config
  options.needs_network → `Agent.__init__` 新参数 → spawn lambda →
  UserModeProcess 构造参数（触碰 agent 构造面，非业务零改动）；
- **编辑边界**：本卡只做参数通道（声明字段 + 传递 + 挂载参数构造——
  消费 sandbox-wrapper 已预留的 `_bwrap_args(..., needs_network=False)`
  单函数），不碰 run()/re-entry exec 骨架与传输层（process.py 内
  wrapper 已提交 b83bda6，基于其上编辑，串行无冲突）。

## 验收

- 未声明 → 沙箱禁网（`--unshare-net`），联网命令失败；
- 声明后 → 沙箱可联网；仅对声明进程生效。

## 依赖

sandbox-wrapper（已提交 b83bda6，串行）；被 bash-sandbox-adapt 消费。
