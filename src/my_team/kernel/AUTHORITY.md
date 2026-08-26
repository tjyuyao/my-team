# Authority：认证系统——决策记录与开放问题

> 本文回答两个问题：**Authority 现在做了什么、怎么做的**（已定），以及
> **哪些地方还没想清楚、为什么**（开放问题）。面向"以后回来一眼看懂"，
> 写得直白啰嗦，不复述代码。

## 0. 一句话定位

Authority 是内核态设备（与 kernel 同进程，可信系统服务），是**组织事实的
唯一权威源** + **框架自带的认证系统**（Django 式）：谁注册了（身份）、
谁声明了什么能力（工具）与什么权限范围（scope）、谁能看到什么
（position + 多粒度 grant）、agent 拓扑是什么。kernel 不持有这些数据，
只做"身份 → 进程句柄"的路由；要组织数据或认证裁决就直调 Authority 问。

## 1. 数据（全内存，kernel 重启即失，见开放问题 7）

- `_identities: {identity → {tools, scopes, agent, position}}`——身份注册表。
  - `tools`：设备声明的工具定义列表（来自工作目录设备源码的 `TOOLS`）。
  - `scopes`：设备声明的权限范围列表（`SCOPES` 导出）——每项
    `{token, default, explanation}`。**token 是不透明字符串**（页级只读、
    角色、类 api-key 凭证……语义设备自定，Authority 不解释）。
  - `agent`：布尔。True = 认知主体，False = 设备。
  - `position`：**认证主体**（岗位）。仅 agent 注册（权限主体只有
    Agent；设备是执行载体，无权限身份，见 §2 原则 7）。
- `_grants: {position → set[(device, token)]}`——布线表（grant 表）。
  授权粒度是 `(position, device, token)`，不限于设备级。
- `_injected: {agent → {name → entry}}`——上次注入快照，diff 出 evict。

## 2. 认证模型（已定，框架自带）

四条规则：

1. **grant 主体 = position，不是身份 uuid**。组织事实，换人不换岗；
   身份只是 position 的持有者（直派形态：agent 以自身为 position，
   默认用法）。
2. **多粒度 scope**。授权可以到整个设备（安装时 `grants: [position...]`
   展开为设备的**默认公开** scope，`SCOPES` 里 `default: true` 的项），
   也可以到具体范围（运行期 `grant_scope` 授 `(position, device, token)`，
   如 `page:42:read`）。动态 scope（运行期新建的页面）按设备的命名方案
   在授予时铸 token，Authority 只存不解释。
3. **权限的最终解释权在设备**。设备通过工具说明（TOOLS 的 description）
   和注入记忆的书面说明（SCOPES 的 explanation → type=skill 条目）作
   书面的使用与权限解释；agent 调用工具执行的是设备定义的代码；设备
   按调用时认证上下文自己裁决。**设备不维护角色系统、不签发证书**——
   认证是框架的事。
4. **Authority 也管理自己的 ACL（人事权）**。命令面仅内核可调（外部
   事件响亮拒绝）；root 或持有 org scope 的 position 经 kernel 系统命令
   使用组织能力：`install/uninstall`（需 `org:install`）、`grant/revoke`
   （需 `org:grant`）。org scope 的授予仅 root 可做。`root` 是启动根
   position（隐式全权）。**root 无特殊**：它只是持有全部 org scope 的
   普通 position（数据，可被持特权者降权/转移）；权限结构唯一不变量 =
   **单调自举**——变更权限（含 root 本身）需已持特权者（org:grant 即
   sudo 式委托）；误降权恢复 = 重装级（Owner 改种子/重建）。
5. **配置 = 安装镜像（种子）**。config 首次启动翻译为运行期数据（agent
   注册、root 授予、初始 grants、workdir 绑定）后即弃；此后一切运行期
   变更都是数据操作（命令 + 动态授权），不存在"编辑配置"的合法形态。
   现状为过渡态：种子充当幂等安装脚本（每次重启重放）；"种子只生效
   一次"需运行期状态持久化（开放问题 7，方向）。
6. **沙箱与权限解耦（v0.14 已实现）**。FS 层（挂载矩阵）与 Authority
   （业务布线）**分离**：挂载矩阵 = 静态出生定格，按身份类型展开为两锚点
   （家 `data/<id>` 可写 + 源码区 `data/devices` 按类型可写/只读），
   data 根 tmpfs 掩蔽其它家（设备数据物理不可见，只经接口暴露）；
   **不承载权限**——无按 position 的挂载物化，设备进程永远不是 root。
   设备分界（`INSTANCE` 声明）：`per-agent` 执行载体（实例
   `<device-id>@<agent-id>`，挂载 = 绑定 agent 的家，命令落 agent 家）/
   `shared` 数据服务（单实例，按 auth 裁决）。跨区数据访问 = 调用级裁决
   （目标设备按发起 Agent 的权限）。
7. **权限主体 = Agent（Linux 用户语义）**。执行动作的一定是发起调用的
   Agent；设备是执行载体（不注册 position）。需要自主权限的服务 =
   专门的服务账户 Agent。Agent 间委托 = 请求，被委托者事先具备权限
   （无权限借出）。富化上下文 = 发起 Agent 的 (position, scopes)。
   设备间调用（未来）：设备不能发起，只能作为某 Agent 调用的执行延伸，
   转发携带 origin（发起者声明），被调方按 origin 裁决。

## 3. 调用时认证（富化）——已定

kernel 路由到设备（非 agent）的事件时，经 Authority 进程内直调解析调用者
（= 发起 Agent；当前直接调用者即 Agent，语义一致）的 `auth = {position,
scopes}`，附加到事件上再投递。宿主侧解析、无伪造面、
零 IPC（Authority 与 kernel 同进程）。设备本地做 scopes 成员检查
（`page:42:read ∈ scopes`）即可裁决。**自查通道未建**（YAGNI）：富化 +
本地成员检查与自查表达力相同，且更经济；将来若出现"非调用时查询/查其他
主体"的具体需求，再开窄的 `has_scope` 成员查询（Authority 只答集合包含，
不解释语义）。

## 4. 命令面（全部为 kernel 的进程内直调；外部事件一律 denied）

| 命令 | 谁调、何时调 | 干什么 | 回执 |
|---|---|---|---|
| `register_request` | `setup()`、`_install` | 登记身份 + 工具/scope 声明 + position | VOID |
| `unregister_request` | `_uninstall` | 撤销登记，连带撤销该身份的全部布线 | VOID |
| `grant_request` / `revoke_request` | `_install` 展开、`_scope` | 登记/撤销 `(position, device, token)` | VOID |
| `authorize_request` | 系统命令前（装卸/grant/revoke） | 裁决身份可否执行某 org scope | 数据回执 |
| `auth_request` | 路由富化 | 返回身份的 `(position, scopes)` | 数据回执 |
| `inject_request` | 装卸/授权后重注入 | 构造注入事件 | 事件回执 |
| `agents_request` | 装卸/授权后枚举 | 查 agent 名单 | 数据回执 |

外部经 `kind=application` 直接投递给 Authority 的事件 → `denied` 事件
回告（响亮），命令面不对进程开放。

## 5. 注入（agent 记忆）

- **工具条目**（type=tool）：position 的 grants 覆盖设备的工具定义——
  "工具说明"作书面的使用方法。同名工具按注册序后注册者胜。
- **skill 条目**（type=skill）：已授且已声明的 scope 的书面说明
  （`explanation`）——"技能记忆"作书面的权限解释；无 trigger，不参与
  匹配，只作知识。
- 条目结构镜像 MemoryEntry（tool is memory entry）；entry_id 稳定锚点
  （`tool:{device}:{name}` / `perm:{device}:{token}`），防注入 churn
  腐蚀引用。

## 6. 与 legacy 的关系（演进方向）

legacy（`src/legacy/`）是两层 Grant（成员 Grant(agent, position) + 能力
Grant(position, entity)）+ 岗位图 + Authority 调用时裁决。当前实现：
能力层 + 直派形态 + **设备内部裁决**（推翻 legacy 的 Authority 裁决）+
富化认证上下文。成员层、岗位图、priority 分级未做。

## 7. 开放问题（未决，勿静默实现）

1. ~~命令面双入口~~ **已解决**：命令面仅内核可调，外部经系统命令 +
   Authority 自身 ACL（root/人事权）。
2. **fail-fast 契约破口**——KernelModeDevice"respond 抛错 = kernel 失败"
   只在直调路径成立；外部事件经 BucketDispatcher 投递时异常被吞进废弃
   task（bucket 假死到下一事件）。命令面关闭后外部触发面变小，但 Journal
   等经事件路径投递的路径仍在。方向：路由到内核态设备的外部事件直接
   响亮拒绝。
3. **双删除语义**——条目带 `deleted_at`（软删），驱逐走 evict 名单
   （硬删）。未裁决正字法。
4. **同名工具冲突**——当前静默 last-wins（注册序，确定性）；legacy 主张
   同名合并、associated 累加。合并语义未决。
5. **TOOLS/SCOPES 形状校验**——`_load_module` 校验导出存在与 token
   非空，不校验其余字段；形状问题可能延迟暴露（半安装态已由注册前校验
   缓解）。
6. **Authority 侧宽容**——未知 command 返回 VOID（静默吞）。agent 侧
   VOID 宽容是定案；Authority 侧未议（倾向响亮报错）。
7. **持久化（升格为种子模型的必然前提）**——配置=安装镜像要求"种子
   只生效一次"，运行期状态（注册/grants/注入）必须跨重启存活；当前三张
   表全内存，重启重放种子 + workdir（过渡态：种子=幂等安装脚本）。持久化
   落地后种子才真正只跑一次。方案未定（Journal 重放 vs Authority 快照）。
8. **注入分级**——legacy grant 带 priority（`<10` 固定 / `≥10` 召回）；
   当前无，注入全量。接 N4 注入管线时引入。
9. **两层 Grant / 岗位图**——成员层（共享岗位、换人不换岗）与岗位层级
   边。当前直派退化形态。参考 `src/legacy/models/position.py`。
10. **org scope 词汇**——当前定义了 `install` / `grant`（已用）与
    `register` / `inject` / `agents`（词汇预留，无命令消费）。需要时
    接入。
11. **设备间调用与 origin（未来）**——设备不能发起调用，只能作为某
    Agent 调用的执行延伸；转发携带 origin（最初发起 Agent 的声明），
    被调方按 origin 裁决；信任模型 = 设备是发起者的可信执行载体。
    当前无设备间调用用例，机制随需求引入（跨区数据访问走调用级裁决，
    不做 FS 物化授权）。

## 8. 已验证的行为（故事测试）

- 认证系统全链路：富化上下文、运行期 grant/revoke、ACL 拒绝、人事权
  授予、kernel-only 命令面、skill 条目（`tmp/check_auth.py`）。
- 布线故事：两岗互相不可见、按岗可见、卸载撤销（`tmp/check_wiring.py`）。
- 冲突确定性：同名工具后注册者胜，跨 PYTHONHASHSEED 稳定
  （`tmp/check_collision.py`）。
- 缺 grants：安装失败回告、无残留（`tmp/check_nogrant.py`）。
- 自举演示：装/卸/重装全程可用（`tmp/demo/run_demo.py`）。
- 沙箱验证：pidns 防杀兄弟、ipcns 隔离（SysV shmget）、per-position
  矩阵一致（无 per-position 物化）、伪造 source 读侧盖章覆盖、重装旧
  通道不残留（`tmp/check_sandbox_verification.py`）。
