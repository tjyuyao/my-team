# My-Team

AgentOS：agent 的操作系统。

## 架构

- **kernel**：内核负责事件调度、协议校验与进程生命周期。
- **agent**（进程）：Agent 是内心自持的进程，记忆与决策都在进程内。
- **device**（进程）：Device 是为其他进程提供能力的服务进程。

进程是基本计算单元，agent 与 device 都是进程，彼此只经事件通信。
宿主只做路由，不读进程内部状态。

## 事件协议

事件 = `(source, target, kind, payload)`：

- **source**：source 由宿主侧注入，标识事件的来源。
- **target**：target 由发送方填写，内核依据它路由事件。
- **kind**：kind 分为两层，system 承载内核语义，application 承载业务语义。
- **payload**：payload 存放任意内容。

所有事件在路由前都经过校验。

## 进程模型

- `Process`：Process 是运行在子进程中的实体，它循环接收事件、作出响应并产出结果。
- `ProcessHandle`：ProcessHandle 是宿主持有的进程代理，通过它与进程交互。
- `Emitter`：Emitter 是进程产出的通道，由它注入事件来源。

事件只来自进程的产出。

## 实现

- `kernel/agent_os.py`：AgentOS。
- `kernel/process.py`：Process。
- `kernel/process_handle.py`：ProcessHandle 与 Emitter。
- `kernel/event_protocol.py`：事件 TypedDict。
- `kernel/event_validator.py`：校验规则集。
- `main.py`：入口。
