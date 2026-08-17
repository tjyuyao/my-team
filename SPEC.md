# My-Team Multi-Agent 系统设计 Spec

## 1. 目标与范围

本系统用于模拟一个由多个智能体组成的、基于层级委派和异步通信的协作团队。

系统具有以下核心特征：

1. **主 Agent 负责决策，不直接执行具体业务工作。**
2. 主 Agent 只能使用：
   - `read`
   - `write`
   - `ls`
   - 委派子 Agent 的能力
3. 子 Agent 可以继续通过 E-mail 委派工作给下属 Agent。
4. Agent 之间的职责关系在日常运行期间构成一棵静态树。
5. 每个 Agent 拥有：
   - 独立私人工作空间
   - 持久化私密记忆
   - 可访问的共享知识库权限
   - 收发 E-mail 的能力
6. Agent 之间通过异步 E-mail 协作。
7. 多个 Agent 在同一离散时间步长内并行运行。
8. 共享知识库支持权限控制和互斥锁。
9. 人类用户可以：
   - 调整模拟时间步长
   - 暂停或恢复系统
   - 向任意 Agent 发送 E-mail
   - 查看系统状态与协作过程

---

# 2. 核心设计原则

## 2.1 决策与执行分离

主 Agent 不直接完成业务任务，只负责：

- 理解目标
- 拆分任务
- 选择下属 Agent
- 发送委派请求
- 接收结果
- 判断是否需要重试、补充委派或调整方案
- 汇总最终决策

子 Agent 负责：

- 执行具体工作
- 读取和写入自己的私人空间
- 访问共享知识库
- 委派下属 Agent
- 通过 E-mail 报告进展和结果

## 2.2 层级静态，任务动态

Agent 的组织关系在一个运行实例中固定不变：

```text
Root Agent
├── Research Agent
│   ├── Web Research Agent
│   └── Data Analysis Agent
├── Planning Agent
└── Review Agent
    └── Quality Check Agent
```

允许动态变化的是：

- 当前任务
- 委派请求
- 任务状态
- E-mail
- 知识库内容
- Agent 的内部记忆
- Agent 的运行状态

不允许在日常运行过程中动态改变的是：

- Agent 的父节点
- Agent 的子节点集合
- Agent 的角色定义
- Agent 的权限上限
- Agent 的私人空间归属

如果需要改变组织结构，应创建新的模拟运行版本，或者由管理员执行显式的拓扑变更操作。

## 2.3 并行执行，离散提交

所有 Agent 在一个时间步内读取同一版本的系统状态，然后并行产生动作。

这些动作不会立即影响其他 Agent 的本时间步执行结果，而是在时间步结束时统一提交。

这样可以避免 Agent 因执行顺序不同而产生不可复现结果。

---

# 3. 系统总体架构

```text
┌─────────────────────────────────────────────┐
│                 Human Control Plane          │
│  调整步长 / 暂停 / 恢复 / 发信 / 查看状态      │
└──────────────────────┬──────────────────────┘
                       │
┌──────────────────────▼──────────────────────┐
│              Simulation Orchestrator         │
│  时间步驱动、事件调度、并行执行、状态提交      │
└───────┬─────────────┬──────────────┬────────┘
        │             │              │
        │             │              │
┌───────▼──────┐ ┌────▼─────┐ ┌─────▼─────────┐
│ Agent Runtime │ │ Mailbox  │ │ Shared KB     │
│ Agent 执行器  │ │ E-mail总线│ │ 权限与锁管理   │
└───────┬──────┘ └──────────┘ └─────┬─────────┘
        │                           │
        ▼                           ▼
┌───────────────┐           ┌────────────────┐
│ Private Store │           │ Lock Manager   │
│ 工作空间/记忆  │           │ 互斥锁/租约     │
└───────────────┘           └────────────────┘
```

---

# 4. 核心实体

## 4.1 Agent

```json
{
  "agent_id": "agent.research",
  "display_name": "Research Agent",
  "parent_id": "agent.root",
  "children": [
    "agent.web_research",
    "agent.data_analysis"
  ],
  "role": "research",
  "status": "idle",
  "runtime_state": "running",
  "capabilities": [
    "read_private",
    "write_private",
    "ls_private",
    "read_shared",
    "write_shared",
    "send_email",
    "delegate"
  ],
  "permissions": {
    "shared_kb_scopes": [
      "project/research/*"
    ],
    "allowed_recipients": [
      "agent.web_research",
      "agent.data_analysis",
      "agent.root"
    ]
  }
}
```

### Agent 必须包含

- 唯一标识符
- 显示名称
- 角色
- 父 Agent
- 子 Agent 列表
- 工具权限
- 共享知识库权限
- 私人空间路径
- 私密记忆存储位置
- 当前状态
- 当前任务
- 邮箱地址
- 生命周期状态

## 4.2 主 Agent

主 Agent 是树根节点，拥有特殊约束：

```json
{
  "agent_id": "agent.root",
  "role": "root_decision_agent",
  "tools": [
    "read",
    "write",
    "ls",
    "delegate"
  ],
  "direct_execution": false,
  "can_delegate": true,
  "can_send_email": true,
  "can_access_all_private_spaces": false
}
```

主 Agent 的 `delegate` 不是普通文件系统工具，而是系统提供的控制能力，用于生成并发送委派 E-mail。

主 Agent：

- 可以读取自己的工作区
- 可以读写自己的私密记忆
- 可以读取被授权的共享知识库
- 可以向直接子 Agent 发送委派 E-mail
- 可以接收来自子 Agent、其他 Agent 和人类的 E-mail
- 不可以直接调用业务工具
- 不可以直接修改子 Agent 的私人空间
- 不可以绕过 E-mail 直接修改下属 Agent 状态

## 4.3 子 Agent

子 Agent 的工具集合由其角色和权限决定。

一个典型子 Agent 可以拥有：

```json
{
  "tools": [
    "read",
    "write",
    "ls",
    "send_email",
    "delegate"
  ]
}
```

更具体的业务工具，例如数据库查询、代码执行、浏览器访问、文件解析等，应作为角色权限显式声明，而不是默认存在。

## 4.4 任务

任务是 Agent 工作的逻辑单位。

```json
{
  "task_id": "task.2026.001",
  "title": "分析市场进入策略",
  "description": "评估三个候选市场并给出进入建议",
  "creator_agent_id": "agent.root",
  "owner_agent_id": "agent.research",
  "parent_task_id": null,
  "status": "assigned",
  "priority": "high",
  "deadline_tick": 40,
  "required_outputs": [
    "market_comparison",
    "risk_summary",
    "recommendation"
  ],
  "created_at_tick": 0,
  "updated_at_tick": 0
}
```

任务状态：

```text
draft
assigned
accepted
in_progress
blocked
waiting_for_children
submitted
reviewing
completed
failed
cancelled
expired
```

## 4.5 E-mail

E-mail 是 Agent 间唯一的正式协作通道。

```json
{
  "email_id": "mail.000123",
  "thread_id": "thread.task.2026.001",
  "from": "agent.root",
  "to": [
    "agent.research"
  ],
  "cc": [],
  "subject": "[DELEGATE] 分析市场进入策略",
  "body": "请评估三个候选市场，并在第 40 个时间步前提交结果。",
  "email_type": "delegation",
  "task_id": "task.2026.001",
  "created_at_tick": 0,
  "deliver_at_tick": 1,
  "status": "queued",
  "priority": "high",
  "requires_reply": true,
  "reply_to": null
}
```

支持的 E-mail 类型：

```text
delegation       委派请求
acceptance       接受任务
progress         进度报告
question         澄清问题
answer           问题答复
result           工作成果
review_request   请求审查
review_result    审查结果
failure          失败报告
blocked          阻塞报告
cancellation     取消通知
human_message    人类消息
system_notice    系统通知
```

---

# 5. 私人工作空间

## 5.1 目录结构

每个 Agent 拥有独立的私人工作空间：

```text
/private/
  agent.root/
    inbox/
    outbox/
    workspace/
    memory/
    task_state/
    logs/

  agent.research/
    inbox/
    outbox/
    workspace/
    memory/
    task_state/
    logs/
```

## 5.2 访问规则

默认情况下：

- Agent 只能访问自己的私人空间
- Agent 不能列出其他 Agent 的私人空间
- Agent 不能读取其他 Agent 的私密记忆
- 父 Agent 不能绕过 E-mail 读取子 Agent 的私人文件
- 子 Agent 不能绕过 E-mail 直接修改父 Agent 的空间

系统管理员可以拥有审计权限，但管理员访问必须生成审计记录。

## 5.3 私密记忆

每个 Agent 有持久化私密记忆，跨模拟时间步和暂停恢复保存。

记忆应区分以下类型：

```text
episodic_memory       事件记忆
semantic_memory       事实与知识
procedural_memory     工作方法
interaction_memory    与其他 Agent 或人类的交互经验
task_memory           当前和历史任务状态
```

建议采用追加式日志和压缩快照相结合的方式：

```text
memory/
  events/
    000001.json
    000002.json
  summaries/
    summary_0001.md
  index.json
```

Agent 可以通过自己的 `read` 和 `write` 工具管理私密记忆，但系统应限制：

- 单步写入大小
- 单个 Agent 的总存储量
- 记忆写入频率
- 敏感信息扩散到共享知识库的行为

---

# 6. 共享知识库

## 6.1 目标

共享知识库用于存放团队共享的信息、文档、中间结果和最终成果。

与私人工作空间不同，共享知识库：

- 可以被多个 Agent 访问
- 受路径级和操作级权限控制
- 支持锁定、修改、提交和审计
- 不属于任何单个 Agent

目录示例：

```text
/shared-kb/
  project/
    requirements/
    research/
    planning/
    decisions/
    deliverables/
  reference/
  templates/
  archive/
```

## 6.2 权限模型

采用基于路径、Agent 和操作的权限控制。

权限操作包括：

```text
list
read
create
write
append
rename
delete
lock
unlock
publish
```

权限规则示例：

```json
{
  "scope": "project/research/*",
  "principal": "agent.research",
  "allow": [
    "list",
    "read",
    "create",
    "write",
    "append",
    "lock",
    "unlock"
  ]
}
```

推荐使用“最小权限原则”：

- 研究 Agent 只能修改 `project/research/*`
- 规划 Agent 只能修改 `project/planning/*`
- 审查 Agent 对成果目录拥有只读和审查权限
- 主 Agent 可以读取所有项目目录，但是否能写入应由项目策略决定
- `decisions/*` 可以要求只有主 Agent 或授权审查 Agent 才能发布

## 6.3 互斥锁

共享知识库的写操作必须经过互斥锁。

锁对象示例：

```json
{
  "lock_id": "lock.001",
  "resource": "project/research/market-report.md",
  "owner_agent_id": "agent.research",
  "mode": "exclusive",
  "acquired_at_tick": 12,
  "lease_until_tick": 16,
  "status": "active"
}
```

### 锁规则

1. 同一资源最多存在一个排他写锁。
2. Agent 必须先获得锁，才能执行写操作。
3. 锁有租约期限，避免 Agent 崩溃后永久占用。
4. Agent 可以在租约到期前续租。
5. Agent 完成写入后应主动释放锁。
6. 如果 Agent 在锁持有期间失败，系统在租约到期后自动释放。
7. 未持锁的写请求必须失败，不能自动覆盖。
8. 锁冲突应通过系统 E-mail 通知请求方。

### 推荐提交模型

对共享文件采用：

```text
获取锁
读取当前版本
修改本地副本
写入新版本
执行一致性检查
提交版本
释放锁
```

每个共享资源具有版本号：

```json
{
  "path": "project/research/market-report.md",
  "version": 7,
  "last_modified_by": "agent.research",
  "last_modified_at_tick": 15
}
```

Agent 提交时必须携带读取时的版本号。如果版本号不匹配，提交失败，需要重新读取和合并。

---

# 7. E-mail 委派协议

## 7.1 委派请求

委派只能发送给当前 Agent 的直接子节点。

```json
{
  "email_type": "delegation",
  "task": {
    "task_id": "task.2026.001.a",
    "title": "收集候选市场数据",
    "description": "收集并整理三个候选市场的规模、增速和监管风险",
    "parent_task_id": "task.2026.001",
    "required_output": {
      "format": "markdown",
      "location": "shared-kb/project/research/market-data.md"
    },
    "deadline_tick": 20
  },
  "instructions": {
    "priority": "high",
    "autonomy": "medium",
    "must_reply": true,
    "allowed_shared_kb_scopes": [
      "project/research/*"
    ]
  }
}
```

委派请求至少应包括：

- 任务目标
- 背景
- 预期产出
- 交付位置
- 截止时间
- 优先级
- 是否需要回复
- 允许使用的共享资源
- 成功标准
- 父任务 ID

## 7.2 接受任务

子 Agent 收到委派后，应在规定时间内发送：

```json
{
  "email_type": "acceptance",
  "task_id": "task.2026.001.a",
  "status": "accepted",
  "estimated_completion_tick": 18,
  "questions": []
}
```

如果无法接受：

```json
{
  "email_type": "failure",
  "task_id": "task.2026.001.a",
  "status": "rejected",
  "reason": "所需数据源不在权限范围内",
  "suggested_alternative": "请求 agent.data_analysis 协助"
}
```

## 7.3 子 Agent 继续委派

子 Agent 只能向自己的直接子 Agent 委派。

```text
agent.root
  └── agent.research
        └── agent.web_research
```

`agent.web_research` 不能直接委派给 `agent.planning`，即使它知道该 Agent 存在。

子 Agent 委派出的任务必须：

- 是其自身任务的子任务
- 不超出其权限范围
- 不超过父任务截止时间
- 不违反其角色的委派上限
- 记录 `parent_task_id`
- 将最终结果回传给其直接委派者

## 7.4 工作成果返回

成果通过 `result` E-mail 返回。

```json
{
  "email_type": "result",
  "task_id": "task.2026.001.a",
  "status": "submitted",
  "summary": "已完成三个候选市场的数据收集。",
  "artifacts": [
    {
      "type": "shared_kb_file",
      "path": "project/research/market-data.md",
      "version": 3
    }
  ],
  "limitations": [
    "巴西市场的监管数据只有 2025 年版本"
  ],
  "recommendation": "建议由上级 Agent 进行跨市场比较",
  "created_at_tick": 18
}
```

E-mail 正文适合放：

- 摘要
- 结论
- 风险
- 待决策事项

大体积内容应写入：

- Agent 私人工作空间，或
- 共享知识库

E-mail 中只传递引用、路径和版本号。

---

# 8. 时间步进模型

## 8.1 模拟时钟

系统使用离散时间步：

```text
tick = 0, 1, 2, 3, ...
```

每个时间步对应一个由人类配置的模拟时长，例如：

```text
1 tick = 1 分钟模拟时间
1 tick = 1 小时模拟时间
1 tick = 1 天模拟时间
```

系统运行速度可以与真实时间不同。

配置示例：

```json
{
  "tick_duration": {
    "value": 10,
    "unit": "seconds"
  },
  "simulation_time_per_tick": {
    "value": 1,
    "unit": "hour"
  },
  "mode": "realtime"
}
```

## 8.2 一个时间步的阶段

每个时间步分为以下10个阶段：

```text
Phase 1:  Freeze    — snapshot global state
Phase 2:  Deliver   — deliver emails + generate NEW_EMAIL wake events
Phase 3:  Schedule  — compute ready set from events + agent states
Phase 4:  Observe   — ready agents read snapshot
Phase 5:  Decide    — ready agents generate action plan
Phase 6:  Validate  — validate action plans before execution (pre-validation)
Phase 7:  Act       — execute validated actions, stage effects
Phase 8:  Commit    — atomic commit of all staged effects
Phase 9:  Publish   — generate wake events from committed effects; timeouts
Phase 10: Audit     — record all events
```

### Phase 1：Freeze

系统冻结当前全局状态快照。

所有 Agent 本时间步都基于同一个快照运行。

### Phase 2：Deliver

系统投递所有满足以下条件的邮件：

- `deliver_at_tick <= current_tick`
- 收件人处于可接收状态
- 邮件未被取消

邮件进入目标 Agent 的 `inbox`。

为每个收件人生成 `NEW_EMAIL` 唤醒事件（在 tick t+1 可见）。

### Phase 3：Schedule

调度器根据以下条件计算就绪集合：

- 每个 Agent 的 `WakeCondition`
- 当前待处理的唤醒事件
- Agent 当前状态（只有 IDLE 或 WAITING_FOR_* 且事件已到达的 Agent 才可被调度）

就绪 Agent 的事件被标记为 `CLAIMED`。每个 Agent 每 tick 最多一次 activation。

### Phase 4：Observe

被调度的 Agent 读取冻结快照：

- 新到达的 E-mail
- 当前任务状态
- 自己的私人工作空间
- 自己的私密记忆
- 有权限的共享知识库快照
- 之前持有的锁状态（只有锁持有者能看到自己的 lock_token）
- 系统通知

### Phase 5：Decide

被调度的 Agent 独立生成本时间步动作计划（`ActionPlan`）。

例如：

```json
{
  "agent_id": "agent.research",
  "tick": 12,
  "actions": [
    {
      "action_type": "delegate",
      "tool_name": "delegate",
      "payload": {
        "recipient_agent_id": "agent.web_research",
        "task_title": "收集市场数据"
      }
    }
  ]
}
```

### Phase 6：Validate（Pre-validation）

在执行前验证每个 Agent 的 ActionPlan：

1. **工具权限** — 每个动作使用的工具是否在 Agent 授权列表中
2. **委派目标** — delegate 动作是否指向直接子 Agent
3. **Payload 字段** — 必填字段是否齐全（如 read 需要 path）
4. **激活预算** — 总动作数是否在限制内

未通过验证的动作被标记为失败，不会进入 Act 阶段。

### Phase 7：Act

被调度的 Agent 执行已验证的动作：

- 执行过程中产生的结果属于临时状态，不立即暴露给其他 Agent
- 未通过 Validate 的动作被跳过
- 执行结果被记录到 `ActionResult` 列表

### Phase 8：Commit

系统按确定性规则提交所有阶段化的副作用：

1. 状态转换
2. E-mail 入队
3. 私人空间写入
4. 锁申请和释放
5. 共享知识库提交
6. 记忆持久化
7. 任务状态更新

**注意：** 当前实现为 stub，仅处理邮件队列。完整的事务模型（原子提交、回滚、冲突解决）待实现。

### Phase 9：Publish

从已提交的副作用生成下一 tick 可见的唤醒事件：

- 已提交的邮件 → `NEW_EMAIL` 唤醒事件
- 任务状态变化 → `CHILD_TASK_CHANGE` 唤醒事件给父任务所有者
- 锁释放 → `LOCK_AVAILABLE` 唤醒事件给等待中的 Agent
- Deadline 检查 → `DEADLINE_APPROACHING` 或 `TIMER_EXPIRY` 唤醒事件

### Phase 10：Audit

系统记录：

- Agent 的输入快照
- Agent 的动作
- 工具调用
- E-mail
- 任务状态变化
- 知识库版本变化
- 锁事件
- 错误和超时
- 人类控制操作
- Agent 激活事件

## 8.3 Tick 语义澄清

**`tick` 是模拟世界推进一次的最小离散时间单位。**

以下概念不等价于一个 tick：

| 概念 | 含义 | 与 tick 的关系 |
|------|------|---------------|
| API 请求 | 一次原始 LLM/API 调用 | 可能在 tick 内完成，也可能跨越 tick |
| LLM 调用 | 一次模型推理 | 可能立即返回，也可能延迟数 tick |
| 工具调用 | 一次工具执行 | 可能在同 tick 返回，也可能异步返回 |
| Agent 完整响应 | Agent 对用户请求的最终结果 | 可能跨越数十个 tick |
| 用户请求 | 人类发起的顶层请求 | 可能跨越多个任务和数十个 tick |

一个用户请求的典型生命周期：

```text
用户请求
  ↓ tick 0
Root Agent 第一次思考
  ↓ tick 1
委派 Research Agent
  ↓ tick 2
Research Agent 收到委派
  ↓ tick 3
Research Agent 调用工具
  ↓ tick 4
工具响应到达
  ↓ tick 5
Research Agent 再次思考
  ↓ tick 6
Research Agent 返回结果
  ↓ tick 7
Root Agent 汇总
  ↓ tick 8
Root Agent 最终回复用户
```

Agent 的一次完整响应可以跨越多个 tick，并包含多次 LLM 调用、工具调用、E-mail 交互和等待。

## 8.4 Agent 激活模型

每个 tick 中，Agent 的行为被限制为**一次有限的 activation**：

```text
一次 Observe
→ 一次 Decide
→ 一批有限 Actions
→ 一次 Commit
```

### 核心概念

| 概念 | 含义 |
|------|------|
| Agent Activation | 某个 Agent 被唤醒并执行一次决策周期 |
| LLM Invocation | 一次调用模型 API |
| Tool Invocation | 一次工具调用 |
| Wake Event | 触发 Agent 唤醒的事件 |

### 激活约束

- 每个 Agent 在每个 tick 内最多完成一次 activation
- 每次 activation 最多进行有限次 LLM 调用（默认 1 次，可配置）
- 每次 activation 最多执行有限次工具调用（默认 8 次，可配置）
- 不允许在同一个 tick 内无限执行 `LLM → tool → LLM → tool → ...` 循环

### 唤醒条件

Agent 只有在满足以下条件时才被调度执行：

```text
agent.status == READY
AND (
    收到新 E-mail
    OR 收到工具结果
    OR 子任务状态变化
    OR 锁可用
    OR 重试时间到达
    OR 人类消息到达
    OR 任务 deadline 临近
    OR 定时器到期
)
```

如果唤醒条件不满足，Agent 保持 `IDLE` 或 `WAITING` 状态，不调用 LLM。

## 8.5 执行模式

系统支持两种执行模式，通过配置选择：

### 模式 A：离散异步模式（默认）

每次 LLM 或工具动作都是可观察的事件，下一轮在后续 tick 执行。

```text
tick 10: Agent 调用 LLM，LLM 请求 read("a.md")
tick 11: ToolExecutor 执行 read，生成 ToolResult
tick 12: Agent 被唤醒，读取 ToolResult，再次调用 LLM
tick 13: LLM 请求 write("b.md")，执行 write
tick 14: Agent 再次被唤醒，判断任务完成，发送 result
```

优点：并行语义清楚、可暂停、成本可控、易于审计和重放。

### 模式 B：有界微循环模式

一个 Agent activation 内部允许有限次 `LLM → Tool → LLM` 循环。

```python
execution_mode = "bounded_micro_loop"
max_rounds = 3  # 最多 3 轮 LLM → Tool
```

优点：对简单场景端到端响应更快。

缺点：暂停粒度粗、成本上限不透明、事务边界更复杂。

### 配置

```json
{
  "execution_mode": "discrete_async",
  "max_llm_calls_per_activation": 1,
  "max_tool_calls_per_activation": 8,
  "max_action_budget": 32
}
```

---

# 9. Agent 生命周期

```text
created → initialized → idle
                         ↑   │
                         │   ↓ wake event
                         │  ready
                         │   │
                         │   ↓ scheduler claim
                         │  processing
                         │   ├── waiting_for_llm ──────┐
                         │   ├── waiting_for_tool ──────┤
                         │   ├── waiting_for_child ─────┤
                         │   ├── waiting_for_mail ──────┤
                         │   ├── waiting_for_lock ──────┤
                         │   ├── waiting_for_human ─────┤
                         │   ├── blocked (需要介入)     │
                         │   └── idle (完成) ───────────┘
                         │
                         ↓ unrecoverable
                       failed
                         ↓
                      terminated
```

### 状态分层

| 层级 | 状态 | 说明 |
|------|------|------|
| 系统级 | created, initialized, terminated | 系统管理，不涉及调度 |
| 调度级 | idle, ready, processing | 控制 agent 是否被调度 |
| 等待级 | waiting_for_* | processing 的子状态，等待外部事件 |
| 异常级 | blocked, failed, paused | 需要外部介入或不可恢复 |

## 9.1 状态定义

### 调度级状态

#### `created`

Agent 对象已创建，尚未初始化。

#### `initialized`

Agent 已完成系统初始化（邮箱、私有空间、工具注册）。

#### `idle`

Agent 没有待处理工作。不调用 LLM，不执行 observe/decide/act。

触发条件（idle → ready）：
- 收到新 E-mail（NEW_EMAIL 唤醒事件）
- 收到工具结果（TOOL_RESULT 唤醒事件）
- 子任务状态变化（CHILD_TASK_CHANGE 唤醒事件）
- 锁可用（LOCK_AVAILABLE 唤醒事件）
- 重试时间到达（RETRY_TIMER 唤醒事件）
- 人类消息到达（HUMAN_MESSAGE 唤醒事件）
- 任务 deadline 临近（DEADLINE_APPROACHING 唤醒事件）
- 定时器到期（TIMER_EXPIRY 唤醒事件）
- 系统启动（BOOTSTRAP 唤醒事件）

#### `ready`

Agent 有待处理工作，等待调度器分配 activation。

#### `processing`

Agent 正在执行 activation（Observe → Validate → Decide → Act → Commit）。

### 等待级状态（PROCESSING 的子状态）

#### `waiting_for_llm`

Agent 已提交 LLM 请求，等待模型响应。当前实现中 LLM 调用是同步的，此状态已建模但尚未实际使用。

#### `waiting_for_tool`

Agent 已提交工具调用请求，等待工具结果在后续 tick 到达。

#### `waiting_for_child`

Agent 已委派子任务，等待子 Agent 返回结果。

#### `waiting_for_mail`

Agent 等待特定 E-mail（如人类回复、审查结果）。

#### `waiting_for_lock`

Agent 等待获取共享资源的互斥锁。

#### `waiting_for_human`

Agent 需要人类决策才能继续。

### 异常级状态

#### `blocked`

无法继续执行，需要上级或系统介入。

#### `paused`

系统暂停状态。不推进时间，不执行 Agent 推理。

#### `failed`

Agent 执行失败，且不可恢复。系统可以根据策略重试或终止。

## 9.2 唤醒条件

每个 Agent 维护一个 `WakeCondition`：

```text
event_types: 触发唤醒的事件类型集合
wake_at_tick: 最早唤醒时间
task_ids: 关联的任务 ID
resources: 关联的共享资源
```

Agent 只在满足唤醒条件时被调度。`IDLE` 和各种 `WAITING_*` 状态不会触发 LLM 调用，显著降低计算成本。

## 9.3 调度策略

系统维护一个 ready queue：

```text
每个 tick 只调度满足以下条件的 Agent：
- agent.status ∈ {ready, waiting_for_* 中事件已到达的状态}
- wake_at_tick <= current_tick
- 有新的可见事件
```

对于 `IDLE` Agent：不创建 activation，不调用 LLM，只推进模拟时间。

---

# 10. 主 Agent 决策循环

主 Agent 的基本循环如下：

```text
读取自己的输入
读取当前任务和共享知识
分析团队状态
检查未完成任务
决定是否拆分新任务
决定是否向子 Agent 委派
决定是否追问、催办或取消任务
等待结果
汇总结果
写入决策和记忆
```

主 Agent 不应直接进行业务事实搜集或复杂计算，除非这些内容已经在其可读取的文件或 E-mail 中提供。

主 Agent 的输出主要是：

- 委派 E-mail
- 追问 E-mail
- 任务取消 E-mail
- 审查请求 E-mail
- 最终决策文件
- 对人类的 E-mail 回复

---

# 11. 任务树与组织树

系统必须同时维护两棵树。

## 11.1 组织树

表示 Agent 的固定职责关系：

```text
agent.root
└── agent.research
    └── agent.web_research
```

组织树在运行期间不变。

## 11.2 任务树

表示当前工作分解关系：

```text
task.main
├── task.research
│   ├── task.market-data
│   └── task.regulatory-risk
├── task.planning
└── task.review
```

任务树可以在运行过程中创建、完成、失败或取消，但每个任务的执行者必须符合组织树授权范围。

---

# 12. 人类控制接口

## 12.1 暂停

暂停操作应在当前时间步提交完成后生效，避免中途破坏状态一致性。

```json
{
  "command": "pause",
  "effective": "after_current_tick",
  "reason": "人工检查"
}
```

暂停后：

- 不推进模拟时间
- 不执行新的 Agent 推理
- 已提交的邮件和状态保留
- 人类仍可以查看状态
- 人类可以发送 E-mail
- 管理员可以调整配置

## 12.2 恢复

```json
{
  "command": "resume"
}
```

恢复后从下一个未完成时间步继续。

## 12.3 调整时间步长

时间步长调整可以有两种模式：

### 即时调整

从下一个时间步开始使用新步长。

### 计划调整

从指定时间步开始使用新步长。

```json
{
  "command": "set_tick_duration",
  "value": 30,
  "unit": "seconds",
  "effective_tick": 25
}
```

建议不修改已经完成的时间步，也不回溯重放正在执行的时间步。

## 12.4 人类发送 E-mail

人类可以向任意 Agent 发送 E-mail，但邮件必须明确标记发送者身份：

```json
{
  "email_type": "human_message",
  "from": "human.user_001",
  "to": [
    "agent.root"
  ],
  "subject": "补充要求",
  "body": "请优先考虑成本约束。",
  "deliver_at_tick": 18
}
```

人类邮件：

- 进入 Agent 的正常邮箱
- 不能绕过权限直接修改 Agent 状态
- 可以要求 Agent 执行任务
- 是否具有更高优先级由运行策略决定
- 所有内容都应写入审计日志

---

# 13. 并发与一致性

## 13.1 读一致性

同一时间步内，Agent 默认看到的是同一版本的系统快照。

如果某 Agent 在本时间步修改了共享知识库，其他 Agent 最早在下一个时间步看到该修改。

## 13.2 写冲突

共享知识库写冲突处理顺序：

1. 检查权限
2. 检查锁
3. 检查资源版本
4. 检查文件格式和 schema
5. 提交新版本
6. 记录审计事件

任何一步失败，都不得产生部分写入。

## 13.3 邮件顺序

同一收件人在同一时间步收到多封邮件时，建议按以下顺序排列：

1. 系统通知
2. 人类邮件
3. 高优先级邮件
4. 截止时间更近的任务
5. 邮件创建时间
6. `email_id` 字典序

邮件顺序只影响 Agent 的观察顺序，不应改变系统提交的一致性规则。

---

# 14. 失败处理

## 14.1 Agent 执行失败

当 Agent 推理或工具调用失败时：

```json
{
  "agent_id": "agent.research",
  "tick": 20,
  "failure_type": "tool_error",
  "retryable": true,
  "error": "共享文件写入失败",
  "affected_tasks": [
    "task.2026.001"
  ]
}
```

系统可以：

- 在本时间步回滚该 Agent 的未提交动作
- 保留已提交的 E-mail
- 自动重试
- 标记任务为 `blocked`
- 通知父 Agent
- 请求人类介入

## 14.2 子 Agent 超时

如果子任务超过截止时间：

1. 系统将任务标记为 `expired`
2. 向任务所有者发送 `system_notice`
3. 任务所有者可以重试、重新委派或降级处理
4. 父任务不应自动标记为完成

## 14.3 锁超时

锁租约到期后：

- 自动释放锁
- 生成锁超时审计事件
- 通知锁持有者和等待者
- 未提交的本地修改保留在 Agent 私人空间
- Agent 可以重新获取锁并尝试提交

## 14.4 邮件无法投递

邮件无法投递时：

```text
queued → delivery_failed → retrying → delivered
                              └── permanently_failed
```

默认重试策略：

- 指数退避
- 最大重试次数
- 超过次数后通知发件人
- 不重复发送已经确认成功的邮件

---

# 15. 权限和安全

## 15.1 权限边界

权限检查必须在系统层执行，不能依赖 Agent 自律。

禁止 Agent 通过以下方式绕过权限：

- 猜测其他 Agent 的私人路径
- 修改 E-mail 发件人
- 伪造系统邮件
- 直接调用内部存储接口
- 使用共享知识库作为私人数据的替代品
- 修改任务树中的执行者
- 修改自己的权限

## 15.2 能力传递

Agent 委派任务时，可以传递有限的工作授权，但不能传递超出自身权限的能力。

例如：

```text
父 Agent 能读 project/research/*
子 Agent 最多只能获得 project/research/source/*
```

委派授权应满足：

```text
子 Agent 有效权限 ⊆ 委派者有效权限
```

此外，系统可以对每个任务附加更窄的资源范围。

## 15.3 审计

所有以下事件必须可审计：

- Agent 创建和终止
- 委派
- 邮件发送、接收和投递
- 工具调用
- 文件读写
- 共享知识库修改
- 锁获取、续租和释放
- 权限拒绝
- 人类操作
- 时间步推进和暂停
- Agent 失败和自动重试

---

# 16. API 草案

## 16.1 创建模拟运行

```http
POST /simulations
```

```json
{
  "name": "market-entry-study",
  "tick_duration": {
    "value": 10,
    "unit": "seconds"
  },
  "agent_tree": "configs/market-entry-team.json",
  "shared_kb_policy": "configs/market-entry-permissions.json"
}
```

## 16.2 推进时间步

```http
POST /simulations/{simulation_id}/ticks/advance
```

```json
{
  "count": 1
}
```

## 16.3 暂停或恢复

```http
POST /simulations/{simulation_id}/pause
POST /simulations/{simulation_id}/resume
```

## 16.4 发送人类 E-mail

```http
POST /simulations/{simulation_id}/mail
```

```json
{
  "to": ["agent.root"],
  "subject": "新增约束",
  "body": "预算不得超过 100 万。",
  "deliver_at_tick": 10
}
```

## 16.5 查看组织树

```http
GET /simulations/{simulation_id}/agents/tree
```

## 16.6 查看任务树

```http
GET /simulations/{simulation_id}/tasks/tree
```

## 16.7 查看共享知识库锁

```http
GET /simulations/{simulation_id}/shared-kb/locks
```

---

# 17. 配置文件示例

```json
{
  "simulation": {
    "tick_duration": {
      "value": 10,
      "unit": "seconds"
    },
    "simulation_time_per_tick": {
      "value": 1,
      "unit": "hour"
    },
    "start_paused": false,
    "deterministic_mode": true
  },
  "agents": [
    {
      "agent_id": "agent.root",
      "role": "root_decision_agent",
      "parent_id": null,
      "children": [
        "agent.research",
        "agent.planning",
        "agent.review"
      ],
      "tools": [
        "read",
        "write",
        "ls",
        "delegate"
      ],
      "can_delegate": true
    },
    {
      "agent_id": "agent.research",
      "role": "research_manager",
      "parent_id": "agent.root",
      "children": [
        "agent.web_research",
        "agent.data_analysis"
      ],
      "tools": [
        "read",
        "write",
        "ls",
        "send_email",
        "delegate"
      ],
      "can_delegate": true
    },
    {
      "agent_id": "agent.web_research",
      "role": "web_researcher",
      "parent_id": "agent.research",
      "children": [],
      "tools": [
        "read",
        "write",
        "ls",
        "send_email",
        "web_search"
      ],
      "can_delegate": false
    }
  ],
  "policies": {
    "max_delegation_depth": 5,
    "email_delivery_latency_ticks": 1,
    "default_lock_lease_ticks": 4,
    "max_retries": 3,
    "private_storage_limit_mb": 512
  }
}
```

---

# 18. 关键不变量

系统实现必须保证以下不变量：

1. 一个 Agent 只有一个父 Agent，根 Agent 除外。
2. 组织关系不能形成环。
3. Agent 只能向直接子 Agent 委派。
4. 委派任务必须属于委派者当前任务或其子任务。
5. Agent 只能访问其授权的私人空间和共享知识库路径。
6. 共享资源写入必须持有有效互斥锁。
7. 同一资源不能同时存在两个排他写锁。
8. 已完成时间步的状态不能被普通 Agent 修改。
9. Agent 之间的正式协作必须通过 E-mail。
10. Agent 不能伪造其他 Agent 或人类的身份。
11. 暂停状态下不能自动推进模拟时间。
12. 所有状态改变都必须可以通过审计日志重建。
13. 组织树在一次模拟运行中保持静态。
14. 私密记忆跨时间步持久化，但默认不对其他 Agent 可见。
15. 共享知识库中的内容必须拥有明确版本号。

---

# 19. 最小可运行闭环

一个最小系统可以按以下顺序实现：

## Phase 1：基础运行时

- Agent 组织树
- 私人工作空间
- 基本 `read`、`write`、`ls`
- 时间步推进
- Agent 状态机

## Phase 2：E-mail 协作

- 邮箱
- 异步投递
- 委派协议
- 任务树
- 结果返回

## Phase 3：共享知识库

- 路径权限
- 版本控制
- 排他锁
- 冲突检测
- 审计日志

## Phase 4：人类控制

- 暂停
- 恢复
- 修改时间步长
- 人类 E-mail
- 状态查看

## Phase 5：可靠性

- 超时
- 重试
- Agent 崩溃恢复
- 锁租约
- 邮件重投
- 确定性回放

---

# 20. 推荐的系统行为总结

一次完整协作过程应类似于：

```text
人类发送目标
  ↓
Root Agent 读取目标并分析
  ↓
Root Agent 通过 E-mail 委派给直接子 Agent
  ↓
子 Agent 接受任务并拆分子任务
  ↓
子 Agent 向自己的下属发送 E-mail
  ↓
多个 Agent 并行执行
  ↓
Agent 将中间成果写入私人空间或共享知识库
  ↓
写共享知识库前获取互斥锁
  ↓
Agent 通过 E-mail 返回摘要和成果引用
  ↓
上级 Agent 汇总并审查
  ↓
Root Agent 形成最终决策
  ↓
Root Agent 写入决策文件或回复人类
```

这个设计将系统划分为四个相互独立但协同工作的核心机制：

- **静态组织树**：约束职责和权限边界
- **动态任务树**：表示实际工作分解
- **异步 E-mail 总线**：实现 Agent 协作
- **离散时间步运行时**：保证并行执行、状态一致性和可回放性
