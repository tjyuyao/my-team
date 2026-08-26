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

**一切皆数据**：协议与内核机制之外不存在"声明"——配置种子是安装镜像
（首次启动翻译为运行期数据后即弃，不维护、不被运行期改写）；设备源码、
工具与权限声明、身份与授权全是数据，修改权限完全动态（Authority 授予）。
权限结构唯一的不变量是**单调自举**：变更权限（包括 root 本身）的能力
只授予已持特权者（org:grant 即 sudo 式委托）；root 无特殊——它只是一个
持有全部 org scope 的普通 position。

## 事件协议

事件 = `(source, target, kind, payload)`：

- **source**：宿主侧注入，标识来源——不可冒充。
- **target**：发送方填写，内核依它路由；必须指向已注册进程。
- **kind**：`system`（内核语义，payload.command 在白名单：
  terminate / install_device / uninstall_device / grant_scope /
  revoke_scope）| `application`（业务语义）。
- **payload**：任意内容。

所有事件路由前经校验；校验失败响亮丢弃并记录，不中断内核循环。
进程契约：`respond(event) -> Event | VOID`——VOID 为合法沉默，不上总线。

## 进程模型

- `Process`：一套契约（respond: Event | VOID），两态同构、可迁移——
  **用户态**（真实子进程，隔离、崩溃不牵连内核）与**内核态**（与 kernel
  同进程托管，可信系统服务，well-known 身份）接口完全一致，传输是唯一差异。
- `ProcessHandle`：宿主持有的进程代理（用户态），经事件总线交互。
  终止契约：`await terminate(timeout)` 返回时进程必已死亡——投终止事件、
  异步等待（不阻塞内核循环）、超时强杀后收尸；**绝不同身份并存**（宿主
  仅以 identity 区分进程，同 identity 双活即协议污染）。
- `Emitter`：进程产出通道，由它注入事件来源。

事件只来自进程的产出。

**分桶并发（所有设备的核心约束）**：设备按 source 分桶处理请求——
**同一来源排队串行（FIFO 保序），不同来源并行**。同一请求方的事件是
同一工作流的连续步骤（如 agent 的 LLM 请求与工具请求交错），乱序会破坏
依赖；不同请求方相互独立，并行隔离。Agent 自身串行处理消息（并发=1，
语义而非配置）。**同源并行不是设备层的功能**——请求方需要并行时在其
请求语义内自行解决（如 bash 命令用 `nohup &`/`setsid`）。
`max_concurrent_sources` 只限制跨源并发的 source 数（0 = 无限），
不改变同源保序。

## 内核态设备

- Authority、Journal 与 kernel 同进程托管；kernel 对其请求为**进程内直调**
  （不经事件路由），其产出仍走统一事件路径（校验 → 记录 →
  路由）——"事件只来自进程产出"不因直调而破坏。
- 内核态身份（authority / journal / kernel）是 kernel 特权的唯一例外：
  不经 Authority 登记，且装卸裁决拒绝触碰。其余一切身份（agent/device）
  的组织事实归 Authority。

## 组织（Authority）

组织事实的**权威源**是 Authority（内核态设备）：身份登记与撤销、能力与
权限范围声明、注入内容、agent 拓扑。kernel 不持有组织数据，只物化执行
所需的 identity → handle 路由映射——注册即向 Authority 申请、物化由
kernel 完成。

**认证系统（框架自带，Django 式）**：Authority 是认证系统，grant 主体 =
**position**（组织事实，换人不换岗），授权粒度 = **多粒度 scope**——
`(position, device, token)`，token 为设备声明的不透明字符串（默认公开 /
页级只读 / 角色 / 类 api-key 凭证），**语义由设备解释**，Authority 只存
不解释。

- 注册/撤销：`register_request` / `unregister_request`（身份 + 能力声明 +
  position + scope 声明）。
- **布线（grant 表）**：`grant_request` / `revoke_request` 登记
  `(position, device, token)`；**deny-by-default**——注入内容 = agent 的
  position 的 grants 覆盖的设备/scope。安装时 `grants: [position...]`
  展开为设备的**默认公开** scope（设备级便捷）；运行期 `grant_scope` /
  `revoke_scope` 系统命令细粒度调整（重注入生效）。position 来自 config
  `options.position`（单一声明源），直派形态为默认用法。
- **Authority 自身 ACL**：命令面**仅内核可调**（外部事件响亮拒绝）；外部
  使用经 kernel 系统命令（install/uninstall/grant/revoke），kernel 先查
  `authorize_request`——position 为 `root`（隐式全权）或持有 org 设备上
  对应 org scope（人事权，`org:install` / `org:grant` 等）；org scope 的
  授予仅 root 可做。
- **调用时认证（富化）**：kernel 路由到设备的事件附加调用者的
  `auth: {position, scopes}`（宿主侧解析，无伪造面）——设备按自己的语义
  裁决权限，解释权在设备。
- 注入：按布线汇总设备声明的工具条目（type=tool）+ 已授 scope 的书面
  说明（type=skill 条目）——"工具说明 + 技能记忆"作书面的使用与权限解释；
  设备卸载/撤销布线后重注入，diff 出 `evict`。
- **Journal 查询权限**（既定原则，最小示例未实现）：Journal 可被 agent
  查询（memory_search 等系统能力条目，associated 指向 journal），查询
  权限经由 Authority 管理——Agent 对 Journal 的读取不是无条件的。
- 演进方向（未实现）：两层 Grant（agent→position 成员授予）、岗位图、
  grant priority 注入分级（`<10` 固定工作记忆 / `≥10` 触发器召回）——
  见 `kernel/AUTHORITY.md`。

## 工作目录与设备装卸

- **工作目录**：Root（agent）的私有文件系统区域（team 配置
  `options.workdir`），存放设备实现源码（`devices/*.py`）等运行期产物；
  源码即持久化形态——重启后重新 bootstrap 即恢复，无需重新生成。
- **设备源码约定**：导出 `Device`（进程类）与 `TOOLS`（工具定义声明），
  可选 `SCOPES`（权限范围声明：`{token, default, explanation}`——token 为
  不透明权限名，default 标记安装时默认公开，explanation 为注入记忆的书面
  说明）。`Device` 实例在**子进程内构造**（父进程只传装载描述，pickle
  无关类对象），spawn/fork 启动方式皆可。
- **装卸**：`install_device`（身份 + 源码路径 + grants 布线声明）请求内核
  装载，**装卸权经 Authority 裁决**（root 或持有 `org:install`）——动态
  加载 → Authority 登记 → grants 展开为默认公开 scope 布线 → 注入全部
  agent（内容按各 agent 的 position 过滤）；`uninstall_device` 卸载——
  终止进程 → 撤销声明（连带撤销布线）→ 重注入（条目驱逐）。结果一律 ack
  回告请求方，失败不击穿内核。
- **bootstrap**：Root 扫描工作目录批量装载；收齐全部回执后向发起者
  报告结果（agent_result），空目录立即报告不挂起。
- **身份保护**：内核态身份与 agent 身份不可被设备装卸顶替。

## 工作空间与沙箱（治理）

进程私密性的机械化：目录约定 + FS 级强制（Linux 多用户式）。

- **目录约定（约定即默认，零配置）**：设备数据区 = 其源码所在 workdir 的
  `data/<identity>/`（"家目录"）；workdir 是 agent 私有区（`devices/`
  源码 + `data/` 自数据）。访问矩阵：系统路径只读；自己的数据区读写；
  他人数据区不可见；设备进程可读自己的源码（加载实现用）。制造例外：
  agent 写 `devices/*.py` 发生在自己的工作区（数据面操作），装好后进程
  即被隔离。
- **沙箱（方向，未实现）**：所有设备进程 bwrap + setrlimit（user
  namespace，无需 root）——系统路径只读挂载、自己的数据区为唯一写根、
  默认禁网（需网络设备显式声明）。FS 挂载矩阵 = **Authority 授予的 FS
  权限在进程出生时物化**：position root 的进程可访问全部数据区（uid 0
  语义）；跨区访问 = 显式 grant（数据区是可授予资源，随需求引入）；
  出生定格——授权变更后需重装进程生效（如 Linux 改组成员需重新登录）。
- **配置与数据的分界（种子即安装镜像）**：配置文件只承载"安装时翻译"
  的内容（agent 拓扑、workdir 绑定、root 授予）；首次启动翻译为运行期
  数据后即弃——此后任何运行期变更都是数据操作（命令 + Authority 动态
  授权），不存在"编辑配置"的合法形态（改拓扑 = Owner 的初始化动作，或
  未来 owner-approved 运行期命令如 register_agent）。现状为过渡态：种子
  充当幂等安装脚本（每次重启重放）；"种子只生效一次"需运行期状态持久化
  （见 `kernel/AUTHORITY.md` 开放问题 7，方向）。
- **root 无特殊**：root 是一个持有全部 org scope 的普通 position，由
  同一 grant 机制管理；唯一不变量 = 单调自举（特权变更需已持特权者）；
  误降权的恢复路径 = 重装级（Owner 改种子/重建）。

## LLM 设备

- LLM 生成能力经设备进程提供，设备协议见 `device/llm/PROTOCOL.md`。

## 测试原则

- **只写故事测试**：每系统域 3-5 个，验证行为结果；协议细节随故事覆盖，
  不单独成测。协议变动不要求测试重写。
- **契约测试须显式批准**：字段级测试默认禁止，经 Maintainer 批准方可写。
- **全量一次跑完**：共享装配 + 事件驱动等待，全量耗时与真实行为时间同量级。（当前仍处于并将长期处于探索期）

## 实现

- `kernel/agent_os.py`：AgentOS（含内核可寻址：install/uninstall_device 裁决）。
- `kernel/process.py`：Process。
- `kernel/process_handle.py`：ProcessHandle 与 Emitter。
- `kernel/event_protocol.py`：事件 TypedDict。
- `kernel/event_validator.py`：校验规则集。
- `device/llm/`：LLM 设备与请求/响应协议。
- `main.py`：入口。
