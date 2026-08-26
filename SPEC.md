# My-Team

AgentOS：agent 的操作系统，代码中如需简写统一使用 `aos` 而不是 `os` 代指。

## 架构

- **kernel**：内核负责事件调度、协议校验与进程生命周期，托管内核态设备
  （Authority 组织注册/注入、Journal 记录——可信系统服务与 kernel 同进程）；
  自身可寻址（`target="kernel"`），承接设备安装/卸载的裁决。
- **agent**（进程）：Agent 是内心自持的进程，记忆与决策都在进程内。
- **device**（进程）：Device 是为其他进程提供能力的服务进程。

进程是基本计算单元，agent 与 device 都是进程，彼此只经事件通信。
宿主只做路由，不读进程内部状态。

## 设计取向

以**全面的动态性**为回报，接受反直觉的代价：系统层也是数据——设备实现
与工具定义（name/description/parameters/trigger）不在配置中静态声明，
而由 Root 在工作目录生产、经 install/uninstall 事件热装卸（源码即持久化
形态）；组织事实归 Authority（权威源），kernel 只物化路由映射并裁决装卸；
Process 是一套契约（respond: Event | VOID），用户态（子进程）与内核态
（与 kernel 同进程）同构、可迁移。每一步都离 AGI 近一步。

## 事件协议

事件 = `(source, target, kind, payload)`：

- **source**：宿主侧注入，标识来源——不可冒充。
- **target**：发送方填写，内核依它路由；必须指向已注册进程。
- **kind**：`system`（内核语义，payload.command 在白名单：
  terminate / install_device / uninstall_device）| `application`（业务语义）。
- **payload**：任意内容。

所有事件路由前经校验；校验失败响亮丢弃并记录，不中断内核循环。
进程契约：`respond(event) -> Event | VOID`——VOID 为合法沉默，不上总线。

## 进程模型

- `Process`：一套契约（respond: Event | VOID），两态同构、可迁移——
  **用户态**（真实子进程，隔离、崩溃不牵连内核）与**内核态**（与 kernel
  同进程托管，可信系统服务，well-known 身份）接口完全一致，传输是唯一差异。
- `ProcessHandle`：宿主持有的进程代理（用户态），经事件总线交互。
- `Emitter`：进程产出通道，由它注入事件来源。

事件只来自进程的产出。

## 内核态设备

- Authority、Journal 与 kernel 同进程托管；kernel 对其请求为**进程内直调**
  （不经事件路由），其产出仍走统一事件路径（校验 → 记录 →
  路由）——"事件只来自进程产出"不因直调而破坏。
- 内核态身份（authority / journal / kernel）是 kernel 特权的唯一例外：
  不经 Authority 登记，且装卸裁决拒绝触碰。其余一切身份（agent/device）
  的组织事实归 Authority。

## 组织（Authority）

组织事实的**权威源**是 Authority（内核态设备）：身份登记与撤销、能力声明、
注入内容、agent 拓扑。kernel 不持有组织数据，只物化执行所需的
identity → handle 路由映射——注册即向 Authority 申请、物化由 kernel 完成。

- 注册/撤销：`register_request` / `unregister_request`（身份 + 能力声明）。
- 注入：设备声明的能力汇总为工具条目（type=tool），经 `inject` 事件注入
  agent 精炼层；设备卸载/撤销声明后重注入，diff 出 `evict`。
- **Journal 查询权限**（既定原则，最小示例未实现）：Journal 可被 agent
  查询（memory_search 等系统能力条目，associated 指向 journal），查询
  权限经由 Authority 管理——Agent 对 Journal 的读取不是无条件的。
- 第一版无 ACL/布线控制：全部设备能力注入全部 agent（结构预留）。

## 工作目录与设备装卸

- **工作目录**：Root（agent）的私有文件系统区域（team 配置
  `options.workdir`），存放设备实现源码（`devices/*.py`）等运行期产物；
  源码即持久化形态——重启后重新 bootstrap 即恢复，无需重新生成。
- **设备源码约定**：导出 `Device`（进程类）与 `TOOLS`（工具定义声明）。
  `Device` 实例在**子进程内构造**（父进程只传装载描述，pickle 无关
  类对象），spawn/fork 启动方式皆可。
- **装卸**：Root 经 `install_device`（身份 + 源码路径）请求内核装载——
  动态加载 → Authority 登记 → 注入全部 agent；`uninstall_device` 卸载——
  终止进程 → 撤销声明 → 重注入（工具条目驱逐）。结果一律 ack 回告请求方，
  失败不击穿内核。
- **bootstrap**：Root 扫描工作目录批量装载；收齐全部回执后向发起者
  报告结果（agent_result），空目录立即报告不挂起。
- **身份保护**：内核态身份与 agent 身份不可被设备装卸顶替。

## LLM 设备

- LLM 生成能力经设备进程提供，设备协议见 `device/llm/PROTOCOL.md`。

## 实现

- `kernel/agent_os.py`：AgentOS（含内核可寻址：install/uninstall_device 裁决）。
- `kernel/process.py`：Process。
- `kernel/process_handle.py`：ProcessHandle 与 Emitter。
- `kernel/event_protocol.py`：事件 TypedDict。
- `kernel/event_validator.py`：校验规则集。
- `device/llm/`：LLM 设备与请求/响应协议。
- `main.py`：入口。
