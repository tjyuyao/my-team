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
- **编辑边界**：本卡只做参数通道（声明字段 + 传递 + 挂载参数构造），
  不碰 sandbox-wrapper 的 run()/re-entry exec 骨架（process.py 内
  不相交的区域）；若实现时边界重叠，改与 wrapper 串行。

## 验收

- 未声明 → 沙箱禁网（`--unshare-net`），联网命令失败；
- 声明后 → 沙箱可联网；仅对声明进程生效。

## 依赖

∥ sandbox-wrapper；被 bash-sandbox-adapt 消费。
