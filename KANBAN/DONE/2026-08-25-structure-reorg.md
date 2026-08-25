---
kind: task
phase: v0.11 agent-impl
source: SPEC §1.7（三态定义）；Owner 2026-08-25 定：全量三态分离是 v0.11 质量前提
priority: highest
---

# 全量三态分离（高内聚低耦合，非文件夹改名）
> **废弃（2026-08-25，Owner 定）**：v0.11 计划整体归档（见
> `KANBAN/PLAN/2026-08-25-v0.11.0-plan.archived.md`），本卡随计划
> 一并废弃。原因：重构方案重议——原「增量功能 + 事后结构重排」
> 路线不满足三态质量前提（测试绿 ≠ 结构正确）。本卡内容留档备查，
> 不执行；新方案见 `docs/THREE_STATE_REFACTOR_PLAN.md`。


> **2026-08-25 Owner 定案**：这不是目录重排，是**架构职责分离**。
> 三态是 v0.11 质量前提：内核（纯逻辑，零业务数据）/ 设备（数据+
> 工具+ACL+锁）/ Agent（内心/头脑/双手）。simulation.py（5178 行
> 上帝类，101 方法）是病根——十阶段引擎 + 全部接线 + 状态序列化
> 混在一个类里。

## 病根（数据）

- `simulation.py` 5178 行 / 101 方法：11 个 `_phase_*`（十阶段引擎）
  + 全部子系统接线 + `_collect_state/_restore_state` 序列化 + 工具注册
- N4 记忆代码写在根目录（memory_store/memory_recall/consolidation/
  context_compiler），未归 Agent 引擎面
- 只有 `devices/`、`models/` 两个子目录，47 个扁平 .py

## 目标结构

```
src/my_team/
  kernel/      # 纯逻辑（不持有状态）：tick 十阶段引擎、事务回滚、
               # ACL 求值、执行真理、Journal 写入逻辑
  device/      # 数据+工具+ACL+锁：kb/records/assets/credentials/mail/
               # tasks/org/config/integration/calendar + authority
  agent/       # 内心/头脑/双手：memory_store/recall/consolidation/
               # injection(context_compiler)/contract(agent_runtime)/
               # continuation/llm_agent/private_store/agent_tools
  models/      # 数据模型（保持）
  simulation.py  # 降级为组装器：拼装 kernel+devices+agents，不实现它们
```

## 拆分批次（每批全量回归再放行）

1. **B1 内核抽取**：十阶段引擎从 simulation 抽为 `kernel/tick_engine.py`
   （状态进出，不持有）；transaction/journal/ACL 求值归 kernel/。
   风险最高，先做。
2. **B2 Agent 归位**：memory_store/memory_recall/consolidation/
   context_compiler→injection/agent_runtime→contract/llm_agent/
   private_store/agent_tools 归 agent/。
3. **B3 设备归位**：devices/ → device/，store 文件改名（kb/records/
   assets/credentials/mail/tasks），Journal 留 kernel/ 不改名。
4. **B4 simulation 降级**：只剩组装（构造 + run_tick 委托内核 + 接线）。

## 验收标准

- [ ] 内核模块不持有业务状态（状态进出参数）
- [ ] 设备无跨设备直连（依赖经接口声明）
- [ ] Agent 引擎内聚（记忆/注入/continuation 同面）
- [ ] simulation 无业务逻辑实现（纯组装）
- [ ] 全量测试绿；ruff/mypy 干净
