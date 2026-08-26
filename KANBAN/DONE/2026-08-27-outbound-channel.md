---
kind: task
status: completed
phase: v0.14
source: SPEC.md
priority: medium
---

# 出站通道读侧盖章（source 构造事实）——已并入 sandbox-wrapper

## 内容

source 由写侧盖章改为**内核读侧盖章**：子进程内不再存在可改写身份字段
（Emitter 变纯队列写入器）；宿主直投路径（测试/宿主显式 source）保留。

**收编说明**：本卡原定"每进程一个出站队列 + 内核 step 多队列轮询 +
按队列归属盖章"的 mp.Queue 注册表机制，随方案 B（sandbox-wrapper 定案）
被 **socketpair + 宿主 reader 线程盖章**取代——传输层重写为 fd 继承
Connection 后，reader 线程按连接归属盖章即等价实现，且天然无死队列残留
（进程终止 → EOF → reader 退出，无需注册表注销）。验收语义全部由
sandbox-wrapper 的 b83bda6 满足。

## 验收（由 b83bda6 满足，已核对）

- 恶意设备尝试伪造 source（payload 带 source 字段 / 改 emitter 属性）→
  宿主 reader 按连接归属盖章覆盖；
- 子进程对象图中无身份字段可改写（ChildWriter 无 identity）；
- 宿主直投（显式 source）行为不变；Journal 的 source 正确；
- 全量回归不回归（b83bda6 全绿，零脚本改动）。

## 依赖

并入 sandbox-wrapper（已提交 b83bda6）；收口故事（伪造 source 覆盖、
重装同身份旧通道不残留）由 sandbox-verification 承接。
