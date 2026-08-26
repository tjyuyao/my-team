# Authority：布线控制——决策记录与开放问题

> 本文回答两个问题：**Authority 现在做了什么、怎么做的**（已定），以及
> **哪些地方还没想清楚、为什么**（开放问题）。面向"以后回来一眼看懂"，
> 写得直白啰嗦，不复述代码。

## 0. 一句话定位

Authority 是内核态设备（与 kernel 同进程，可信系统服务），是**组织事实的
唯一权威源**：谁注册了（身份）、谁声明了什么能力（工具）、谁能看到谁的
能力（布线 grant 表）、agent 拓扑是什么。kernel 自己不存这些数据，只维护
"身份 → 进程句柄"的路由映射；要组织数据就直调 Authority 问。

## 1. 数据（全内存，kernel 重启即失，见开放问题 7）

- `_identities: {identity → {tools, agent, position}}`——身份注册表。
  - `tools`：设备声明的工具定义列表（来自工作目录设备源码的 `TOOLS` 导出）。
  - `agent`：布尔。True = 认知主体（agent），False = 设备。
  - `position`：agent 的布线主体（岗位）。**设备没有 position**。
- `_grants: {position → set[device_identity]}`——布线表（grant 表）。
  记录"某岗位能看到哪些设备"。
- `_injected: {agent → {tool_name → entry}}`——上次注入给每个 agent 的
  条目快照，用来 diff 出本次要逐出的 evict 名单（增量写协议，见 §4）。

## 2. 命令面（全部为 kernel 的进程内直调，见 OUT_OF_BAND.md）

| 命令 | 谁调、何时调 | 干什么 | 回执 |
|---|---|---|---|
| `register_request` | `setup()`（agent 拓扑）、`_install`（设备装载） | 登记身份 + 能力声明 | VOID |
| `unregister_request` | `_uninstall` | 撤销登记，**连带把该身份从所有 position 的布线中摘除** | VOID |
| `grant_request` | `_install` 装载成功后，对 `payload.grants` 逐条 | 登记一条布线 (position, entity) | VOID |
| `inject_request` | `_install`/`_uninstall` 之后对全部 agent 逐个 | 构造注入事件（见 §4） | 事件回执 |
| `agents_request` | `_install`/`_uninstall` 开始前 | 查当前全部 agent 身份 | 数据回执 |

直调的特点：不走事件路由、不经协议校验、不进 Journal；事件里的
`"source": "system"` 是纯形式（`"system"` 不在注册表里，走事件路径必被拒）。
所以直调是"内核特权通道"，也是唯一合法的调用方式——**但**命令面本身对
外部事件也是开放的，见开放问题 1。

## 3. 布线模型（已实现，v1）

核心规则一句话：**deny-by-default**——agent 能看到哪些工具，完全由
"自己的 position 在 grant 表里布线了哪些设备"决定；没布线的设备即使已
注册、已在跑，对任何 agent 都不可见。这条规则消灭了早期"全部设备能力
注入全部 agent"的做法。

几个具体决定：

- **position 从哪来**：config 里 `agents[].options.position`，唯一声明源。
  kernel 的 `setup()` 从 options 读它去注册；Agent 进程构造时也从同一个
  options 拿到它（自举时要用它声明 grants）。两份消费、一个来源，不会漂移。
  agent 注册缺 position = 配错，启动期 fail-fast。
- **直派形态**：v1 没有独立的"岗位"实体，position 就是一个字符串，
  默认等于 agent 自己的身份（agent `main` 的 position 就是 `"main"`）——
  语义是"我装的设备归我自己的岗"。将来要引入组织化岗位（共享岗位、
  换人不换岗、岗位层级）时，只改 position 的赋值方式，机制本身不动。
- **安装即布线，卸装即撤线**：`install_device` 必须携带
  `grants: [position, ...]`（非空列表；缺失则整次安装失败回告
  `ok=False`，且不留任何注册残留）。内核装载登记后逐条发 `grant_request`。
  **没有独立的"授权/撤权"命令**——布线与设备生命周期绑定，这是唯一做法。
- **卸载连带撤销**：`unregister_request` 把该设备从所有 position 的布线中
  摘除；随后对全部 agent 重注入，diff 出 evict——失去布线的 agent 的工具
  条目被逐出精炼层。

一个完整的例子（单 agent `main` 自举天气设备）：

```
main 扫描 workdir/devices/utils.py
  → 发 install_device(identity="utils", grants=["main"])
kernel:
  → 校验/加载源码（约定导出 Device 与 TOOLS）
  → register_request(utils, tools=[weather,time], agent=False)
  → grant_request(position="main", entity="utils")
  → 对每个 agent: inject_request(main) → 布线命中 utils → 注入条目
main 收到 inject 事件 → 精炼层有了 weather/time 条目 → 任务可匹配
```

## 4. 注入机制

- 流程：`inject_request(agent)` → 查该 agent 的 position 的布线 → 汇总
  可见设备声明的工具 → 与 `_injected` 快照 diff → 返回 inject 事件
  （`entries` = 全量新视图，`evict` = 本次要移除的名字）→ kernel 路由给
  agent → agent 写入/逐出精炼层。
- **条目结构**：完全镜像 MemoryEntry（"tool is memory entry"理念——工具
  就是记忆条目的一种，不是独立机制）：
  `entry_id / type="tool" / content{name,description,parameters} /
  trigger / priority(默认 10) / associated=[device_id] / version=1 /
  links=[] / deleted_at=None`。
- **entry_id 稳定锚点**（定案 S4）：`tool:{device}:{name}`——工具定义没变
  则 id 不变。理由：条目 id 是 links/associated 等引用的锚点，若每次注入
  都换新 id，注入 churn 会把引用腐蚀掉。
- **同名工具冲突**：按**注册序**聚合，后注册者胜（确定性已恢复；曾因
  布线改用 set 迭代变成掷骰子，已修）。合并语义（legacy 主张同名合并、
  associated 累加）未决，见开放问题 4。

## 5. 与 legacy 的关系（演进方向）

legacy（`src/legacy/`）是完整的两层 Grant 模型：
`Grant(agent, position)`（成员：agent 占据岗位）+ `Grant(position, entity)`
（能力：岗位可见设备），且岗位之间有层级边（superior/subordinate）、
grant 带 priority（`<10` 固定工作记忆 / `≥10` 触发器召回）。

v1 只做了**能力层 + 直派形态**（即本文 §3 所述）。成员层、岗位图、
priority 分级都是未决演进，见开放问题 8、9。

## 6. 开放问题（未决，勿静默实现）

1. **命令面双入口**——Authority/Journal 可寻址，外部进程发
   `kind=application` + 组织命令（register/unregister/inject/agents/grant）
   也能到达：协议校验只拦 `kind=system` 的命令白名单，不拦
   `kind=application` 的 payload 内容。后果：今天任何设备都能摘掉别家的
   声明、覆写任意身份、对任意 agent 触发注入。布线控制已把**注入内容**
   收口（deny-by-default），但**命令面本身**仍对等开放。方向：命令级区分
   "内核专用 vs 外部可调"，或等岗位化 ACL 落地后由授权裁决。
2. **fail-fast 契约破口**——KernelModeDevice 的定案是"respond 抛错 =
   kernel 失败"（fail-fast，不允许带病运行）。但这个契约**只在直调路径
   成立**：外部事件经 BucketDispatcher 投递时，respond 抛错会被吞进
   "永不检索"的废弃 task，该 source 的 bucket 假死到下一个事件才复活，
   kernel 本身毫无感知。实现与定案矛盾。方向：路由到内核态设备的外部事件
   直接响亮拒绝（与开放问题 1 一并收口）。
3. **双删除语义**——条目带 `deleted_at`（软删，保持引用稳定），但驱逐
   走独立的 evict 名单（硬删，条目直接消失）。两种删除语义并存，没有
   裁决哪个是正字法。方向：统一为墓碑式（evict = 置 deleted_at）或明确
   evict 名单为正字法并删掉 dead 字段。
4. **同名工具冲突**——legacy 定案是"同名合并、associated 累加"（多实例
   池化语义）；当前实现是静默 last-wins（确定性已恢复：按注册序聚合，
   后注册者胜）。合并语义仍未决。
5. **TOOLS 形状校验**——`_load_module` 只校验"模块导出了 `Device` 和
   `TOOLS` 两个名字"，不校验 TOOLS 条目的形状；缺 `name` 等字段会延迟到
   inject 时才 KeyError——而那时设备已注册完成（半安装态：进程在跑、
   工具没注、ack 却报失败）。方向：装载时校验条目形状，失败即拒绝安装。
6. **Authority 侧宽容**——未知 command 返回 VOID（静默吞掉）、
   unregister 用 `pop(x, None)`（对不存在的身份静默成功）。agent 侧的
   VOID 宽容是定案；Authority 是内核态可信服务，是否同样宽容未议
   （倾向：未知命令响亮报错）。
7. **持久化**——`_identities`/`_grants`/`_injected` 全内存，kernel 重启
   即失。恢复路径 = setup（config 拓扑）+ bootstrap（工作目录设备重装）+
   重建布线，理论自愈。无落盘讨论。方向：若"重启自愈"够用则不落盘，
   否则 Authority 状态进 Journal/独立存储。
8. **注入分级**——legacy 的能力授予带 priority（`<10` 固定工作记忆预算 /
   `≥10` 触发器召回）；当前 grant 表无 priority，注入全量。接 N4 注入
   管线（预算/召回）时引入。
9. **两层 Grant / 岗位图**——成员层 `Grant(agent, position)`（共享岗位、
   换人不换岗）+ 岗位层级边（superior/subordinate）。当前是直派退化形态。
   参考 `src/legacy/models/position.py` 与 legacy devices/authority.py。

## 7. 已验证的行为（故事测试）

- 单 agent 自举：装/卸/重装全程工具可用（`tmp/demo/run_demo.py`）。
- 布线故事：两岗互相不可见、按岗可见、卸载撤销布线（`tmp/check_wiring.py`）。
- 冲突确定性：同名工具后注册者胜，跨 PYTHONHASHSEED 稳定
  （`tmp/check_collision.py`）。
- 缺 grants：安装失败回告、无注册残留（`tmp/check_nogrant.py`）。
