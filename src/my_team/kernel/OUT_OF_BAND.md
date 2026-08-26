# 内核直调（特权通道）记录

本文档记录 kernel 对内核态设备的**直调操作**：kernel 侧不经事件总线、
不经校验、不经 Journal 记录，直接调用内核态设备 `respond` 的调用。
这是 kernel 与系统设备（Authority/Journal）之间的特权专用通道。

## 清单（当前实现）

| 位置 | 命令 | 触发路径 | 回执类型 | 失败处理 |
|---|---|---|---|---|
| `register` | `register_request` | setup()（agent 拓扑）、_install（设备装载） | VOID（单向登记） | 无 try（启动期，fail-fast） |
| `_install`/`_scope` | `grant_request` | 装载后的默认 scope 展开（每 position/token 一次）、运行期授权 | VOID（单向登记） | try 内 |
| `_scope` | `revoke_request` | 运行期撤权 | VOID | try 内 |
| `_authorize` | `authorize_request` | 系统命令前（装卸/grant/revoke）的认证裁决 | 数据回执（allowed） | try 内 |
| `_enrich` | `auth_request` | 路由富化（每设备事件一次） | 数据回执（position+scopes） | 无 try（解析失败=路由失败） |
| `_inject` | `inject_request` | 装卸/授权后注入循环 | 事件回执（注入条目，转交 `_kernel_emit`） | try 内 |
| `_uninstall` | `unregister_request` | 设备卸载 | VOID | try 内 |
| `_agents` | `agents_request` | 装卸/授权后身份判定 + 注入枚举 | 数据回执（agent 列表） | try 内 |
| `_record` | `journal_record` | `_process_event` 每个事件（校验成败都记） | VOID | 无 try |

对照项（**不**是直调）：`_kernel_emit`（产出走统一事件路径）、
`_on_kernel`（target=kernel 经事件分发）、`_route` 中
`_kdispatchers[target].submit`（外部事件投递到内核态设备，经分桶）。

## 特征

1. **特权通道：绕过三件套，伪 source 标识。** 全部直调不经校验、不经
   Journal 记录、不经路由；请求 dict 的 `"source": "system"` 是纯形式——
   `"system"` 不在 entities，走事件路径必被 `SourceRegistered` 拒绝。
   直调是内核请求的唯一可行通道（内核的请求没有合法 source 可走事件
   路径）；当前形态是"内核特权通道"的最简实现。

2. **命令面对外部事件关闭。** Authority 的 respond 开头即拒绝
   `source != "system"` 的事件（`denied` 回告，响亮）。外部使用组织
   能力经 kernel 系统命令（install/uninstall/grant/revoke_scope），
   kernel 以 `authorize_request` 请 Authority 裁决（root 或 org scope）。
   双入口问题由此闭环；Journal 记录的是"被路由的事件"，内核直调仍不可见。

3. **全部同步请求-应答。** 全部 await，无 fire-and-forget。回执四态：
   VOID（4）/ 数据（3：agents/authorize/auth）/ 事件（1）——仅
   `inject_request` 的产物跨回事件路径。

4. **失败语义与 fail-fast 一致，但有一个最强形态。** 直调异常沿调用链
   传播：装卸/授权路径内被 try 转为 ack；setup 期无 try（拓扑配错即
   启动失败，正确）。`_record` 的 Journal 直调无任何保护——sqlite 写失败
   （磁盘满/库损坏）会沿 `_process_event` → `step` 击穿事件循环，整个
   内核停摆。符合 KernelModeDevice 的既定 fail-fast 契约（"respond 抛错
   = kernel 失败"），但代价是**记录器故障 = 系统停摆而非降级**——可议的
   边界（如写失败降级为内存缓冲 + 告警）。

5. **审计口径 = 事件流，不含内核操作流。** 直调自身不进 Journal
   （register 不可见，inject 的产物可见）。Journal 反映"被路由的事件"
   （含富化上下文——记录的是设备实际收到的形态），不反映"内核内部
   操作"——这是口径特征，与"记录内核所见"的定位一致。

## 结论

直调集是一个同质的特权通道族：同构（都是 await respond + system 伪
source）、同源（只从 kernel 发出）、同语义（绕过三件套）。命令面对外部
关闭后（特征 2），外部触达 Authority 的唯一路径 = 系统命令 + 自身 ACL，
设计自洽。两个值得留意的边界：① 路由富化（auth_request）每设备事件一次
进程内解析——成本可忽略，但语义上把认证上下文绑定到了路由时刻；②
Journal 写失败 = 内核停摆（fail-fast 的最强形态，可议降级）。
