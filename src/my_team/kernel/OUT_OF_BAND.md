# 内核直调（特权通道）记录

本文档记录 kernel 对内核态设备的**直调操作**：kernel 侧不经事件总线、
不经校验、不经 Journal 记录，直接调用内核态设备 `respond` 的调用。
这是 kernel 与系统设备（Authority/Journal）之间的特权专用通道。

## 清单（当前实现）

| 位置 | 命令 | 触发路径 | 回执类型 | 失败处理 |
|---|---|---|---|---|
| `register` | `register_request` | setup()（agent 拓扑）、_install（设备装载） | VOID（单向登记） | 无 try（启动期，fail-fast） |
| `_install` | `grant_request` | 设备装载后的布线循环（每 position 一次） | VOID（单向登记） | _install 内 try |
| `_inject` | `inject_request` | _install/_uninstall 注入循环 | 事件回执（注入条目，转交 `_kernel_emit`） | _install/_uninstall 内 try |
| `_uninstall` | `unregister_request` | 设备卸载 | VOID | try 内 |
| `_agents` | `agents_request` | _install/_uninstall 身份判定 + 注入枚举 | 数据回执（agent 列表） | try 内 |
| `_record` | `journal_record` | `_process_event` 每个事件（校验成败都记） | VOID | 无 try |

对照项（**不**是直调）：`_kernel_emit`（产出走统一事件路径）、
`_on_kernel`（target=kernel 经事件分发）、`_route` 中
`_kdispatchers[target].submit`（外部事件投递到内核态设备，经分桶）——
后者与直调形成同一设备的两套入口。

## 特征

1. **特权通道：绕过三件套，伪 source 标识。** 全部直调不经校验、不经
   Journal 记录、不经路由；请求 dict 的 `"source": "system"` 是纯形式——
   `"system"` 不在 entities，走事件路径必被 `SourceRegistered` 拒绝。
   直调是内核请求的唯一可行通道（内核的请求没有合法 source 可走事件
   路径）；当前形态是"内核特权通道"的最简实现。

2. **内部方言：命令集是内核专用协议。** register/inject/unregister/
   agents/journal_record 只被 kernel 调用，语义（如 agents 回执不经
   路由）只在内核侧成立。但 Authority/Journal 可寻址，**外部进程发
   同样命令也能到达**（经分桶投递，无 ACL 拦截）——同一命令双入口、
   两套语义（直调无痕 vs 事件有痕，后者会被 Journal 记录）。第一版
   无 ACL 下为已知边界；未来需在命令级区分"内核专用"与"外部可调"。

3. **全部同步请求-应答。** 6 处全 await，无 fire-and-forget。回执三态：
   VOID（4）/ 数据（1）/ 事件（1）——仅 `inject_request` 的产物跨回
   事件路径。

4. **失败语义与 fail-fast 一致，但有一个最强形态。** 直调异常沿调用链
   传播：_install/_uninstall 内被 try 转为 ack；setup 期无 try（拓扑
   配错即启动失败，正确）。**特例：`_record` 的 Journal 直调无任何
   保护**——sqlite 写失败（磁盘满/库损坏）会沿 `_process_event` →
   `step` 击穿事件循环，整个内核停摆。符合 KernelModeDevice 的既定
   fail-fast 契约（"respond 抛错 = kernel 失败"），但代价是**记录器
   故障 = 系统停摆而非降级**——可议的边界（如写失败降级为内存缓冲 +
   告警）。

5. **审计口径 = 事件流，不含内核操作流。** 直调自身不进 Journal
   （register 不可见，inject 的产物可见）。Journal 反映"被路由的事件"，
   不反映"内核内部操作"——这是口径特征，与"记录内核所见"的定位一致。

## 结论

直调集是一个同质的特权通道族：同构（都是 await respond + system 伪
source）、同源（只从 kernel 发出）、同语义（绕过三件套）。当前设计
自洽。两个值得留意的边界：① 命令集对外部事件开放（无 ACL 期的双
入口）；② Journal 写失败 = 内核停摆（fail-fast 的最强形态，可议降级）。
