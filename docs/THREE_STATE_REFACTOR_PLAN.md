# 三态重构方案（v0.11 重议版）

> **2026-08-25 定稿**：Owner 判定「测试绿 ≠ 结构正确」，v0.11 原计划
> （增量功能 + 事后结构重排）整体废弃。本方案重新定义重构——**以
> SPEC §1.7 三态为唯一正确性标准**，代码结构不符合三态即不正确，
> 与测试无关。
>
> 关联：`KANBAN/PLAN/2026-08-25-v0.11.0-plan.archived.md`（归档）；
> `KANBAN/DONE/` 下 10 张 TODO 全部废弃。

## 1. 正确性的定义（唯一标准）

**SPEC §1.7 三态**：

| 态 | 职责 | 不做什么 |
|---|---|---|
| **内核 kernel** | 纯逻辑：tick 十阶段、事务回滚、ACL 求值、执行真理、认知真理 | 不持有业务状态、不碰数据 |
| **设备 device** | 数据 + 读写工具 + ACL + 锁 | 无跨设备直连、不实现策略 |
| **Agent agent** | 内心/头脑/双手：记忆、注入、continuation、LLM 执行器 | 不持有共享状态 |

判定：**代码的每一行都归得上三态之一，且不越界** = 正确。测试绿只是
行为没变的证据，与正确性无关。

## 2. 当前病根（审查结论，2026-08-25）

- `simulation.py` 5178 行 / 116 方法——上帝类：十阶段引擎（11 个
  `_phase_*`，~3000 行）+ 状态容器（60+ `self._xxx`）+ 组装接线
  （构造/序列化/工具注册）三类职责挤在一个类。
- **阶段逻辑与状态同处一类**：`_phase_commit` 直接操作
  `self._shared_kb/_record_store/_task_tree/...`（30+ 成员）——内核
  逻辑（事务语义）与设备操作（数据读写）未分离。
- N4 记忆代码（memory_store/memory_recall/consolidation/injection）虽
  在本次工作区已物理移入 `agent/`，但**职责边界未清理**（Agent 引擎
  仍经 simulation 间接操作）。
- 本次工作区已有未提交改动：`device/`、`agent/` 目录归位 + kernel/
  WorldPort 引擎重写（测试红，7 失败）。**这些改动只完成了物理搬移
  与编排上移，未完成职责分离**——按三态标准仍不正确。

## 3. 目标结构

```
src/my_team/
  kernel/         # 纯逻辑，零业务状态
    engine.py     #   tick 十阶段引擎（编排 + 阶段语义）
    transaction.py#   事务/回滚/INVERT 契约
    acl.py        #   position 两层 Grant 求值
    executor.py   #   执行器分级/沙箱真理
    journal.py    #   Journal 写入/回滚逻辑
  device/         # 数据+工具+ACL+锁
    kb/records/assets/credentials/mail/tasks/org/config/
    integration/calendar + authority + base
  agent/          # 内心/头脑/双手
    memory_store/recall/consolidation/injection/contract/
    llm_agent/private_store/agent_tools
  models/         # 数据模型（保持）
  simulation.py   # 组装器：构造 + 接线 + 序列化，不含业务逻辑
```

## 4. 重构原则（与旧方案的关键差异）

1. **不做增量**：一次到位。旧方案「先做功能，结构重排放最后」已被
   Owner 否决——那让 N1c/N4 的代码写在错误结构上，累积债务。
2. **不迁就兼容**：测试直接调 `_phase_*` 的（snapshot_matrix 等）
   允许重写；接口命名允许新造（如内核访问协议）；内核文件允许很长。
   唯一约束是三态正确。
3. **职责分离先于物理搬移**：旧改动先 mv 文件再想职责，顺序反了。
   本方案：先定义每段代码归属（对照 §1.7），再动文件。
4. **内核经协议访问状态**：内核不持有状态 → 需要「内核↔组装器」访问
   边界。此边界在 SPEC §1.7 未具名，需**先补 SPEC 再实现**（本次不再
   未确认发明——命名与形式经 Owner 确认后写入 SPEC §1.7 补充节）。

## 5. 执行批次（每批以「三态标准」验收，非测试）

| 批次 | 内容 | 验收（对照 §1.7） |
|---|---|---|
| B1 | 内核抽取：十阶段引擎 + 事务 + ACL + journal 逻辑 → kernel/ | 内核模块不持有业务状态；阶段逻辑不直接碰设备数据 |
| B2 | 设备归位：store 设备化完成 + 无跨设备直连 | 设备只持数据+工具；依赖经接口 |
| B3 | Agent 归位：记忆/注入/continuation 内聚 agent/ | Agent 引擎内聚；不越界碰共享状态 |
| B4 | simulation 降级：只剩组装 + 序列化 | simulation 无业务逻辑实现 |
| B5 | SPEC 补 §1.7：内核访问协议具名 | SPEC 与代码一致 |

每批独立可审（一次 commit），但**不做中间兼容层**——宁可一次大
commit 后全量修测试，不做「先兼容再清理」的两段式。

## 6. 待 Owner 确认的决策点

1. **内核访问协议命名与形式**：内核如何读状态？候选：
   - 协议对象（WorldPort 类——本次已实现雏形，测试红待修）；
   - 纯函数式（十阶段接收状态参数进出，内核零持有）；
   - 依赖注入（内核构造收状态引用，直接访问——简单但不彻底）。
2. **simulation 的去留**：降级为组装器（保留类名/文件）还是拆散
   （run 循环归 runtime、序列化归 persistence）？
3. **B1 是否含「阶段逻辑下沉」**：11 个 `_phase_*` 是整体进 kernel/
   还是按职责拆散（事务语义进内核、工具调用归设备）？后者更纯但
   工作量大数倍。

## 7. 与旧归档的关系

- v0.11 计划、10 张 TODO：全部废弃（DONE rejected），内容留档备查。
- 有效认知保留：N1c 设备化思路、N4 记忆模型（AgentMemory/Recall/
  Consolidation）、Journal 记录而非重放——这些**设计结论**仍有效，
  但**落点与归属**按本方案重新执行。
