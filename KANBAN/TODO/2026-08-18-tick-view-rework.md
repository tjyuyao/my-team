---
kind: task
phase: v0.10 能力
source: SPEC §3.1（已同步，2026-08-18 订正）
priority: high
---

# v0.10-17: 快照模型纠正（每 tick 一轮 + 冻结按需化 + micro loop 废除）

**排期：v0.10 最先**（先于 T8a/T8b/T10/fs-ops——所有新读工具建在读取模型上，
纠正须先行，避免双倍返工）。

## 背景（此卡存在的理由）

现状实现 `_build_snapshot`（simulation.py）每 tick 对**每个 agent 的整棵
私有目录树** `rglob("*")` 逐文件 `read_text` 全文读进内存并算全量哈希——
即"全体资源内容快照"。这是不可接受的成本（O(全部文件内容)/tick），且其
声称服务的需求在"每 tick 一轮 + 锁"下多为冗余：

1. **读一致性**：每 Agent 每 tick 最多 1 次 activation（SPEC §3.1 Schedule
   已有）→ 串行化 → 无并发交错可隔离，快照隔离多余；
2. **冲突检测基准**：只需要 Agent 实际接触路径的 version/hash（按需），
   不需要全量哈希；
3. **外部工具输入**：按需物化输入路径即可。

另：`ExecutionMode.BOUNDED_MICRO_LOOP`（activation.py）是**从未接线的幽灵
模式**（SPEC 中无 §8.5 执行模式章节，其注释是错误引用）——同 tick 内多轮
LLM→Tool 会破坏提交原子性与读取一致性，正式废除。SPEC §3.1 已同步上述
决策（"每 tick 一轮（唯一执行模型）"、"原子提交的来源是串行化"、
"冻结视图按需化"三条原则）。

## 目标
读取模型从"全量内容快照"改为"目录索引 + 按需路径级读取（提交态 + 自己
staged 合并）"；废除 micro loop；行为语义不变（Agent 读到的内容与修改前
一致），性能与实现复杂度质变。

## 实施步骤
1. `_build_snapshot`：全文读取 → 目录/元数据索引 + 状态摘要哈希
   （O(资源数) 的元数据，不含文件全文）；删除 `home.rglob` + `read_text`
   全量循环。
2. workspace_versions：全量内容哈希 → **按需路径级基准**（记录 Agent 实际
   read/write/apply_patch 过的 `path → version/hash`），作为冲突检测基准
   （apply_patch base-hash 与 CommitValidate 仍生效）。
3. read/ls handler：`read_view` 改为"提交态 + 自己本 tick staged（FILE_WRITE/
   FILE_PATCH 未提交者）的按需合并视图"；对外可见语义不变（自己的写同 tick
   可读、他人写不可见）。
4. python_transform 输入：从同一按需视图取（逻辑不变，仅底层换成按需）。
5. 删除 `ExecutionMode.BOUNDED_MICRO_LOOP` 与 `max_micro_loop_rounds`
   （activation.py），修正错误注释引用（SPEC §8.5 不存在）。
6. 相关测试更新 + 全量回归。
7. by-product 确认：本卡**不动** run_tests/git 的 cwd（宿主目录问题归
   "工具执行环境对齐"卡，v0.10 次优先级）。

## 验收标准
- [ ] 无任何"全体文件内容"快照路径（grep 无 `rglob`+`read_text` 于快照构建）
- [ ] read 见提交态 + 自己本 tick staged；他人同 tick 变更不可见（行为不变）
- [ ] 冲突检测基准按需化且 apply_patch/CommitValidate 冲突拒绝仍然生效
- [ ] bounded_micro_loop 不存在于任何源码；SPEC §3.1 三条原则已在
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过；kanban_lint 0 violation