---
kind: task
phase: v0.14
source: SPEC.md
priority: medium
---

# 出站通道读侧盖章（source 构造事实）

## 内容

source 由写侧盖章改为**内核读侧盖章**：每进程一个出站队列，
`{identity → mp.Queue}` 注册表；内核 step 轮询全部队列，按队列归属盖章。
子进程内不再存在可改写身份字段（Emitter 变纯队列写入器）；宿主直投路径
（测试/宿主显式 source）保留。

## 技术要点

- Emitter 删除 identity 属性与盖章（~7 行）；ProcessHandle 创建/注册出站
  队列（+5 行）；UserModeProcess 接口不变（emit 可调用面保留，业务零改动）；
- agent_os：`_outqueues` 注册表 + step 多队列轮询（~20 行）；
  **注册表生命周期：uninstall/terminate 时注销该身份的出站队列**（+3~4 行），
  防死队列常驻轮询与重装同身份残留；
- **宿主直投保留**：`event_bus` 保留为宿主专用队列（显式 source），内核
  只对进程出站队列按归属盖章——tmp 下约 15 个脚本的
  `aos.event_bus.put(显式 source)` 路径不变；
- 内核态设备 `_kernel_emit` 不动（本就在内核侧盖章）；
- 代码 docstring 同步：`event_protocol.py`/`process.py`/`agent_os.py`
  头部"宿主侧（Emitter）注入 source"措辞改为"内核读侧盖章"。

## 验收

- 恶意设备尝试伪造 source（payload 带 source 字段 / 改 emitter 属性）→
  内核按队列归属盖章覆盖；
- 子进程对象图中无身份字段可改写；
- 宿主直投（显式 source）行为不变；Journal 的 source 正确；
- 全量回归不回归。

## 依赖

sandbox-wrapper（同触 process 机制，避免冲突）；被 verification 依赖。
