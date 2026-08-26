# Agent 记忆设计

三层：原始层、精炼层、工作记忆。写入与检索机制各异。

## 三层模型

- **原始层**：长期所有消息在一起（append-only JSONL，忠实不精炼）。
  与工作记忆无直接关系；经专用查询工具（memory_search）检索，
  返回值可 append 到工作记忆末尾。
- **精炼层**：按条目组织（MemoryEntry），是**所有内心记忆**（含私有
  skill、tool）与注入记忆；**工作记忆的唯一召回源**。
- **工作记忆（messages）**：每次决策的上下文。来源三路，全部 append：
  ① 事件流（append-only，为 input cache rate——前缀复用命中缓存）；
  ② 精炼层召回的知识增量；③ 原始层查询结果。

**缓存纪律（硬约束）**：前缀缓存命中 = 请求从开头的连续 token 前缀一致。

- **一切注入一律 append 到 messages 末尾**（召回知识、原始层查询
  结果、事件），绝不插入前缀中间（如 system 之后）——中间插入即
  前缀断裂，其后全部缓存失效。
- **增量原则**：已注入工作记忆的召回条目不重复 append——召回去重，
  防止 messages 膨胀与噪声累积。
- **system 保持稳定**（位于 messages 前，变化即全前缀失效）。
- **tools 位于 payload 末尾**（OpenAI-compatible 键序 model →
  messages → tools，tau 与 DSH 同此布局）：工具列表随记忆自由增删
  不影响 messages 前缀缓存。

## 精炼层条目

```python
MemoryEntry:
  entry_id: str
  type: task | skill | tool | person | ...   # 枚举可扩展
  content: str                               # type-aware 结构（见下表）
  trigger: list[str]                         # 触发器，条目的一部分，可 retag
  priority: int                              # 注入分级：<10 固定工作记忆，>=10 触发器召回
  associated: list[str]                      # 关联对象 id（agent/设备/任务/业务）
  version: int                               # 不可变版本链
  links: list[str]                           # 链接：条目间互引 / 引原始层 / 引外部对象
  deleted_at: str | None                     # 软删除（保持引用稳定）
```

- 写入：整理模式（CONSOLIDATING）产物 + Agent 主动管理（记忆工具）。
- 变更 = 新版本，永不原地改写；删除为软删除。
- 存储：JSONL 持久化（行号即稳定引用，参考 devmem.py）。

**type 的 content 结构**（4.2）：

| type | content | associated 语义 |
| --- | --- | --- |
| task | 任务上下文笔记/进度/决策依据 | 业务/任务 id |
| skill | SOP 文本 + 适用条件 | 相关工具/设备 |
| tool | 工具定义（name/description/parameters） | 设备 uuid（分发目标） |
| person | 档案、关系备注、偏好 | 对方 agent uuid |

**工具条目（type=tool）的动态性**：

- 工具 = 设备能力打包成的工具定义，作为条目存于精炼层——"设备
  记忆注入"的结果（设备装载 + wiring 时写入），不是代码常量。
- **工作目录驱动 + 热装卸**：设备实现与工具定义（name/description/
  parameters/trigger）同处工作目录（`devices/*.py`，Root 生产，源码即
  持久化）；bootstrap 扫描目录 → install_device 装载（grants 声明布线）
  → Authority 注入（inject/evict 事件）→ 工具集合随装卸演化，无需重启。
  能力在设备源码，暴露在注入条目。
- **布线（deny-by-default）**：注入内容 = agent 的 position 所布线的
  设备声明（grant 表）；未布线的设备能力不可见。position 来自 config
  `options.position`。
- `tools=` 每次决策从工具条目动态生成：常驻（priority<10）∪
  召回命中（≥10）的 tool 条目，content 原样进工具列表。
- 分发查条目：tool_call.name → 匹配 tool 条目 content.name →
  associated 设备 uuid → 构造该设备协议事件。执行细节不进条目
  （schema 是知识，事件格式由设备协议定义）。
- 条目可管理：增（注入/promote）、改（retag/更新 content）、
  软删（evict）——工具集合随记忆演化。

**自建工具（promote 产物）的执行**：

- 条目持有：可执行源码 + 依赖的设备列表（associated）。
- `tool_call`（工具名 + 参数）到达 agent 后，agent 把条目引用与
  参数转发给**独立的工具执行进程**。
- 执行进程运行源码；源码中需要外部能力时，经事件协议向
  associated 设备发事件，收集结果后组合成工具结果，返回给 agent。
- 排除两条路径：
  - **agent 逐条发设备事件**：工具调用展开成 agent 侧多步事件序列，
    工具无独立执行体，无法固化复用，promote 无意义。
  - **bash 执行**：bash 语义是执行 shell 命令；工具语义是运行源码
    并编排设备调用，两者不同。
- 执行进程形态（第一版待定）：独立进程（可做成设备，收 `tool_run`
  事件），运行工具源码，经事件调设备，返回结果；源码在独立进程
  执行，避免污染 agent 进程状态。

**priority 注入分级**（4.3）：外加载条目带 priority 分数——

- `priority < 10`：固定工作记忆——按序、持久有效，单独预算、不可超
  （预算可配置，岗位 JD 属此类）。
- `priority ≥ 10`：经触发器召回（普通召回机制）。

## 召回（精炼层 → 工作记忆）

被动召回：状态变化时机制自动触发，不依赖 LLM 自觉。

**触发点**（任一）：

- 新增消息 append 时——该消息自动成为 query。
- queryset 被编辑时——以新 queryset 查询一次。
- 记忆整理结束时——精炼层已变化，刷新召回。

**query 源**：

- 即时：新增消息内容。
- 持久：queryset——agent 可维护的可控查询词；编辑时才影响召回
  结果，平时不重复参与（固定 query 的召回结果固定，增量去重后
  为空转）。

**匹配**：query 词 vs trigger 词。第一版 = 关键词/子串命中 + TF-IDF
加权 × recency 衰减（30 天半衰期）→ top-k。

**注入**：命中且未注入的条目（增量原则）带来源段标签
（如 `[MEMORY:skill]`）append 到工作记忆末尾，受 token 预算约束。

**向量化 = 可插拔后端**：同一接口的另一实现（trigger 向量 vs
query 向量），embedding 源未定前不接。

## 原始层

- append-only JSONL：所有 prompt/response/事件的完整记录。
- **记录口径 = 内核所见**：每个到达事件 + outcome（已路由 / 校验失败 /
  target 未注册等丢弃原因）。协议层事故只入 Journal、不投递业务层
  （无退信——业务层不需要知道协议层事件）。
- 查询工具 `memory_search`：全文/流式检索（按时间、关联对象），
  返回值可 append 到工作记忆（唯一的接入点）。

## 整理模式（CONSOLIDATING）

- 触发：工作记忆超预算或 Agent 主动（consolidate 意图）。
- 回合内 tools 收窄为记忆工具集；多轮工具调用，本地执行。
- 产物：折叠摘要（fold）、提炼长期条目（promote）、trigger 维护
  （retag）、移出工作集（evict）。

## 记忆工具集

| 工具 | 作用 |
| --- | --- |
| memory_fold | 折叠历史为浓缩条目 |
| memory_promote | 提炼为长期条目（可关联 task_id 作 provenance） |
| memory_retag | 维护条目 trigger |
| memory_evict | 移出工作集、保留召回可达 |
| memory_search | 查询原始层 |
| memory_recall | 主动召回（临时 query） |

## 已知边界（当前实现）

- **Agent 硬编码 bash 协议**：`_append`/`_react` 硬编码 `"bash_result"`，
  `_dispatch` 硬编码 `"command": "bash_run"`——第一版仅 bash 工具的自洽
  现状。多设备时改为按工具条目的执行方式构造事件（分发查条目已做，
  事件构造硬编码待泛化）。
- **未知设备结果事件兜底**：非 `bash_result` 的结果事件（如未来
  `file_result`）会被 `_react` 兜底当触发事件 append 成 user 消息——
  支持多工具时须按工具条目识别结果事件，而非命令名。

## 待定 / 演进

- 向量化 embedding 源（OpenAI / 本地模型）。
- 条目 type 枚举的确定范围（第一版先支持哪些）。
- 跨设备 ACL（条目引用外部对象的权限）——未实现。
- 注入布局与审计（"当时它看到了什么"）——若引入 Journal 则记录。
