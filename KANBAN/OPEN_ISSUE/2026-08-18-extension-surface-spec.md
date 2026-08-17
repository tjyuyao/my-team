# 扩展表面规范（Extension Surface）— 设计议题

**Opened:** 2026-08-18
**Kind:** issue（设计审查，未确认为任务）
**Source:** SPEC §6 ToolPlugin / §11 场景包 / §12 安全 / §16 演进
**Status:** OPEN — 持续讨论中

## 设计纪律：场景 = 测试向量

写作工作室、软件公司等场景是**测试向量**，用来检验抽象是否够通用；
**设计输入只有通用不变量**，场景词永远落数据层，不进入原语。
内核抽象稳定实现之前，不实现任何场景资产。

## 背景与诉求

Owner 需要 My-Team 适配新的、多样且"脏"的真实业务场景，同时把自己
的时间从逐单业务改造中解放。收敛后的术语：**扩展表面（Extension Surface）**。

诉求：
1. 扩展 = coding agent（或 root 自进化）面向扩展表面编程，**不改内核源码**；
2. 一套扩展规范，与 My-Team 演进方向强一致；
3. Deployment 可**热插拔**加载配置包，不打断在跑业务。

## 关键术语

- **Deployment**：一个 Customer 专属的 AI 公司实例（**一个 Deployment 服务
  一个 Customer，强业务适配**）；热插拔加载配置的对象。
- **ProcessInstance**：一个 ProcessDef 的一次执行（一个 ticket → 一次流程）。
- **Customer**：个体户的服务对象（非组织成员，经 Contact/ingress 交互）。
- **资产（Package）**：可跨 Deployment 复用/借鉴的配置（组织/角色/流程/Skill/
  知识）。配置经验与知识在**资产层**复用，运行时互不共享。

### 角色 vs 身份（辨析）

- **角色 Role**（组织层·可配置）：业务概念，`manager/engineer/总编/审校`；
  场景包数据，可换。
- **身份 Identity**（闭包·不可配置）：安全概念，`from/to` 身份字段由内核注入，
  Agent 不可自指，不可伪造。
- 一句话：角色是「你干什么活」，身份是「内核如何认定你是你」。

### 四类「人」（LLM 语境都叫"用户"，My-Team 语境须区分）

| 术语 | 是谁 | 在系统中的位置 |
|---|---|---|
| Provider | My-Team 维护/咨询/托管方 | 框架外，能力层大改动 + 咨询 |
| Owner | 个体户老板，Deployment 所有者 | 人类一等参与者，最终决策 |
| Customer | 个体户的客户 | Contact，非组织成员，经 ingress 交互 |
| Human Member | 个体户的人类员工 | 组织树 kind=human 的 Agent（Manager/Worker） |

四者的权限模型、交互界面、事务路径完全不同，必须分开建模。

## 框架：四层 + 一闭包

```
组织层   Org           谁是谁 · 谁向谁汇报 · 谁能做什么
能力层   Capability    员工能调用什么（Tool / Skill / MCP / 受限脚本）
编排层   Orchestration 员工如何协作完成一件事（ProcessDef）
发布层   Release       版本/生效时间/灰度/A-B/回滚
闭包     Closure       内核不变量，永不开放
```

### 组织层 Org
- `roles.json`：角色定义（数据，非原语），其裁决权走统一 Authority 模型（见下）。
- `org_tree.json`：Agent 实例（绑定 role），树结构可配置（深度/扇出/汇报关系）。
- 支持两种生命周期：`persistent`（常驻员工：私有记忆/邮箱/持久状态）与
  `transient`（**临时工/外包**：绑一次任务、无私密记忆、共享工作区、能力受委派者
  控制、任务结束即归档）——对应 coding agent 的 subagent 模式。

### 能力层 Capability（放权梯度）
- 受控脚本（L0/L1）：**放权给 root** 自造，加速业务流程，跑在受限执行+校验+回滚。
- Tool / Skill / MCP / 大改动：**不放权**，由 Provider 扩展。
- 受控 Python 表达式/函数归属此层，不进入编排层。

### 编排层 Orchestration
- 声明式 ProcessDef（图/状态机），不写代码；语言只有结构原语，无业务词汇。
- 门统一原语 + 三种 type：`superior` / `peer` / `human`；终局由 Authority 承接。

### 发布层 Release
- PackageVersion 不可变、多版本并存；`effective_at` 定时生效；`route` 灰度；
  `experiment` A/B；回滚 route 归零。
- 复用内核既有语义（版本/epoch/日历/KPI 投影/回滚）。

### 闭包 Closure（永不开放）
tick/事务/回滚 · **身份注入** · deny-by-default 授权 · TickJournal ·
epoch fencing · 路径安全。

## 通用裁决模型 Authority（内核原语）

任何多主体协作系统都会遇到「多个主体对同一 effect 提出合法主张，冲突时
谁的主张成为绑定结果」。Authority 裁决的是**决策主张（decision claim）**，
不是权限（授权属闭包，deny-by-default）。

**判据（域完备性）**：一个 domain 是「一类具有相同冲突语义、且需要独立
裁决规则的 decision claim 维度」。

### AuthorityGrant 形式

```yaml
authority:
  subject: <role | @owner | @human>      # 主体
  domain: <domain>                       # 域（通用枚举）
  context: <数据层标签，可选>             # 子域限定（如 content 下的某文体字段）
  strength: final | veto | consult       # 强度
  composition: <可选，仅多 final 时>      # priority | joint | threshold
  conditions: <可选>                     # 生效条件（veto 尤其需要）
  escalation: <可选>                     # unresolved 时的升级对象
```

四个维度各司其职，互不塞入彼此：`strength`=能做什么、`composition`=多个终局
如何合成、`conditions`=效力何时生效、`escalation`=无法决策怎么办。

### 域（8 个，通用枚举，非业务词）

| domain | 类型 | 含义 |
|---|---|---|
| `scope` | 构成 | 做什么/不做什么、做到什么范围 |
| `content` | 构成 | 最终产出是什么 |
| `method` | 构成 | 通过什么技术/流程/渠道产生 |
| `schedule` | 构成 | 何时完成/发生、顺序 |
| `cost` | 构成 | 消耗哪些资源、上限 |
| `acceptance` | 评价 | 什么条件下算合格 |
| `release` | 边界 | 是否越界对外生效 |
| `ownership` | 关系 | 归属/责任（谁的产出、谁背锅） |

- `method` 从 content 中显式拆出（「做什么」≠「怎么做」；技术选型/外包自制
  是独立冲突语义）。备选：实用层可合并为 `content := result+representation+
  method`，但这是**有意的合并**，须显式声明而非默认。
- `acceptance` 是评价谓词 `acceptable(effect)=true/false`，非 effect 内禀属性。
- `ownership` 是关系性域（归属/责任标注），非 effect 内禀维度，但组织对其有
  真实冲突，故保留。
- 风险/合规不单列域：建模为 `acceptance` 或 `release` 上的条件 `veto`
  （如「合规负责人对 release 持 veto，conditions=违规」）。

### 强度与合成

强度三档（`none` 为缺省，不声明）：`final` / `veto` / `consult`。

**合成（composition）不是第四档强度，而是多 final 的合成规则**：
- `priority`：有序归约（A final > B final）
- `joint`：联合一致（A AND B 都同意才生效）
- `threshold`：N of M 同意

**默认单终局**：每个 domain+context 最多一个无条件 final。出现多 final 且无
composition 时，状态为 **`unresolved`（不可完备配置）**，运行时
`decision=unresolved`，**不得隐式选胜者**（不随机、不"最后发言者胜"、不
"职位模糊高者胜"、不"先写入者胜"）。

**veto 条件化**：`veto(subject, domain, conditions)`，否则所有 veto 都变成
无限阻塞点；无条件 veto 是显式 `conditions=none` 的特例。`consult` 的
「是否必须被咨询/记录/回应」属流程属性，暂不扩展。

### gate 与 authority 正交

gate 是编排层结构（何时、由谁、判定什么）；authority 是组织层裁决结构
（冲突时、对哪个域、谁终局）。gate 引用 authority（gate 属于某 domain，
分歧时查该 domain 的 final 持有者），而非内置裁决。

## 治理图：六种有类型的关系（授权链统一）

委派、裁决、责任、升级、知情共享同一个**治理图**（节点=主体，边=带类型
标签的关系），但不可合并为一条边——它们的传递/撤销/责任规则不同。

| 关系 | 回答 | 传递 | 撤销 | 关键性质 |
|---|---|---|---|---|
| `assignment` | 谁执行什么 | 可委派 | 可 | 任务义务 |
| `authorization` | 谁能访问什么 | 可授予 | 可 | deny-by-default，属闭包 |
| `authority` | 谁对冲突终局 | 受限投影（单调） | 可 | 域×强度×条件 |
| `accountability` | 谁担责 | **不可下移** | — | 绑定承诺者 |
| `escalation` | 未决事项交谁闭合 | 沿图向上 | 事项闭合即消失 | 转移处置权，非所有权 |
| `information` | 谁知情 | 广播 | — | 不参与裁决 |

**RACI 为校验锚点而非同构**：A（Accountable）≈ 责任+最终决策资格，不等于
全部 final；veto 不在 C 中；R 是执行责任 ≠ 委派关系；I 是信息 ≠ Authority。
保留三个区分：谁被委派执行 ≠ 谁有裁决权；谁担责 ≠ 谁有 Authority；谁知情 ≠ 谁裁决。

**delegation 与 escalation 非严格互逆**：方向相反 ≠ 语义逆运算；委派内容
未必能原样升级回来，升级可能改变上下文/责任/裁决范围。

**四条治理不变量（可验收硬约束）**：
1. 授权不授责：accountability 不随 delegation 下移；
2. veto 默认不可转授（除非显式 + 责任同步转移）；
3. escalation 不改变 ownership（除非 transfer 显式连带）；
4. 委派单调性：下级 authority ⊆ 委派者 authority（呼应 delegation 协议
   已有验收「子 Agent 有效权限 ⊆ 委派者有效权限」）。

## 原语 vs 数据（词汇表二分）

**语言层**（固定、场景无关）：
`process / step / gate / role(槽位) / trigger / transition / predicate /
sla / escalation / version / activation / route / lifecycle /
authority(subject·domain·context·strength·composition·conditions·escalation)`

**数据层**（场景包命名空间，任意命名）：
角色名、步骤 id、工具名、记录类型名、事件类型名、流程名、包名、
content 域下的子类标签。

## 管理学通约（测试向量的来源）

| 现象 | 通用模式 | 原语 |
|---|---|---|
| 挡客户翻译需求 | 缓冲/翻译层 | 入口步骤 |
| 独立把关 | 守门人 | gate |
| 工序串行 | 流水线 | step + depends |
| 背靠背互审 | 同层互审 | gate.type=peer |
| 变更流程 | 变更门 | 流程版本 + 变更 gate |
| 可观测红线/回滚 | 运行保障 | 观测 + 回滚 |
| 绝对决策权 | 责任边界 | authority |

## 热插拔（一等设计目标）

0. 热插拔不止「加载包」，而是**四层任意配置**（组织/角色/流程/裁决/发布策略）
   都可运行中变更，每次变更都是事务 effect + 版本绑定。
1. 安装 = `INSTALL_PACKAGE` 事务 effect，入 TickJournal，可审计、可回滚；
2. 公告走 Email（复用消息模型），不打断其他 tick；
3. **版本绑定**：运行中 ProcessInstance 绑定其实例化时的 schema 版本，
   新配置只影响新实例 —— 「不打断客户业务」的实质保证。

## root 自进化（元编程）

root 生成/改写的是**数据**（ProcessDef/roles/org_tree），非代码——业务结构
已数据化，root 在数据层做元编程。

闭环：观测 KPI → root 生成/改写配置 → 新 PackageVersion → 发布层（校验+灰度）
→ KPI 逐版本对比 → 采纳 / 回滚。

护栏：
1. 自进化只改**组织层 + 编排层 + 发布层**，绝不碰闭包；
2. 初期能力层不放（root 只能重排已有积木，不造新积木；受控脚本除外）；
3. 生成物强制走发布层校验 + 审计 + 可回滚。

「流程生成器」（管理学知识库 + LLM）本身是能力层的一个 Skill：初期作 root 的
受控工具（`propose_process` / `publish_process`），成熟后抽离为 Provider 托管服务。

## 方向：朝 AGI

三个目标是一件事的三面：
- **灵活性（抽象通用）**：决定「什么能变」；
- **热插拔（运行中重配置）**：决定「能否不停机地变」；
- **AGI（自进化）**：决定「谁在变」——由系统自己变自己。

AGI 路径 = **内核极小化 + 其余全部数据化 + 可治理的自进化闭环**：
- 内核（闭包）极小且固定，是唯一「系统不可改变自己」的部分；
- 组织/角色/流程/裁决/能力接线/发布策略全是数据，系统自身（root）可读写；
- 自进化 = root 在数据层元编程（观测 → 生成 → 发布 → 回滚）。

My-Team 的 AGI 不是「失控的自我改写」，而是**可治理的自我进化**：闭包极小、
其余全是可审计可回滚的数据，自我改进的每一步都走事务与发布层。

## 静态校验器（职责边界）

**一句话**：静态校验查「声明的良构性」，动态校验查「动作的合法性」——两者不混。

**三个时机**：加载时（INSTALL_PACKAGE 全量静态）；运行时（内核已有的
PreValidate/CommitValidate，不重建）；生成时（root 自进化产物走同一套加载校验）。

**静态校验五类**：
1. 引用完整性：gate/step 引用的 role、step、check 函数、escalation 目标存在；
2. 结构合法性：组织树无环、depends 无环、domain 是 8 枚举、无死依赖；
3. Authority 一致性：unresolved 检测 + 四条不变量的可静态判定部分；
4. 闭包边界：配置不得触碰 tick/身份/Journal/授权内核/epoch/路径；
5. 版本兼容：新版本 vs 运行中 ProcessInstance 版本绑定。

**不校验**：业务正确性、LLM 实际行为、外部世界状态、不重复内核动态校验。

**四条不变量横跨两层**：声明部分静态查，执行部分动态查（委派单调性在每次
delegate 动作时查、escalation 不转移 ownership 在 effect 层保证）。

**校验器本身是闭包**：不可被配置包替换或绕过。自进化能改四层数据，但永远
改不了校验器——它是「系统能改什么」的守门人。

**产物**：通过 → INSTALL_PACKAGE 原子提交入 Journal；拒绝 → 结构化错误列表 +
整体拒绝（不半装），呼应 SPEC §11.2。

## 开放问题（待议）

- [ ] 编排层 v1 是否需要子流程（subgraph）嵌套，还是先平铺
- [ ] 发布层 route 的分片键先支持哪些
- [ ] ProcessInstance 显式迁移（migrate）语义与安全边界
- [ ] 声明式谓词的表达能力上限，何时必须引用能力层函数
- [x] Authority 收敛为 8 域（含 method）+ composition 三档 + unresolved 语义 —— v1 待确认
- [ ] transient 临时工的授权/记忆/工作区精确边界
- [ ] 能力层放权梯度的判断边界（哪些脚本可放权、哪些必须 Provider）
- [ ] 与 SPEC §6 ToolPlugin / §11 场景包 / Skill 的术语对齐与合并
- [ ] 【落地·已展开】配置包静态校验器（职责边界见正文「静态校验器」节）：五类检查 + 校验器本身属闭包 + 四条不变量静态那半
- [ ] 【落地·已暂存】Authority 解析算法：context 匹配 / composition 求值 / escalation 触发
- [ ] 【落地·已暂存】自进化边界：root 能否改写 Authority？strength/context 可改？escalation 指向 Owner 的链是否只读？
- [x] 【理论·已收敛】consult 的 mandatory / response 子属性（源自 RACI 征询义务 + 说明理由义务）
- [x] 【理论·已收敛】escalation 的 on（unresolved / condition_breached / exception）与 mode（arbitrate / transfer / advise）（源自例外管理 + 安灯 + 授权链双向）
- [x] 【理论·已收敛】治理图六关系（assignment/authorization/authority/accountability/escalation/information）+ 四条治理不变量 —— 理论收官

## 全面审阅结论（2026-08-18）

**总评**：方向通过；作为 v0.11 实现规范**暂缓**；作为设计审查议题**继续**。
最大风险：扩展表面看似开放，实际**运行时语义未严格定义**，仍是"架构原则 +
设计议题"，不是可直接实现的合同。

**保留的核心判断**：内核极小化 + 业务结构数据化 + 自进化受发布层治理。
**补一条同等重要的原则**：所有可变数据必须拥有明确的**版本、生命周期、权限、
恢复、可重放**语义——否则无法长期作为扩展合同。

**P0 语义缺口（阻塞实现）**，按优先级：
1. ProcessDef / Step / Gate / Transition / ProcessInstance schema 未形式化；
2. Authority 未成为可执行裁决算法（claim 如何产生、context 匹配、composition）；
3. pending op 跨 tick 生命周期与"不得跨 tick 孤儿 op"矛盾；Commit→Publish 崩溃
   窗口无恢复；外部不可逆操作无 `unknown`/对账/补偿；
4. 热插拔版本绑定范围不完整（只绑 ProcessDef，缺 ExecutionProfile）；
5. 配置包/能力包/数据包信任边界未闭合（handler 引用混入配置包）；
6. 静态校验器缺可达性/终局/权限单调/敏感数据流/资源上限检查；
7. root 自进化边界（尤其能否改写 Authority / authorization）未定。

**最小测试向量（语义闭合的唯一判据）**：
```
IngressEvent → ProcessInstance → assignment → human approval
→ external irreversible operation → crash recovery
→ compensation / reconciliation
```
这条链路闭合前，不实现五个场景包的资产。

**下一步**：已拆为 `KANBAN/PLAN/2026-08-18-extension-surface-plan.md`
（P0→P1→P2 路线图）与 7 个 P0 TODO（见 `KANBAN/TODO/2026-08-18-*.md`）。

