# v0.9-6: ContextCompiler —— 角色化观察与 token-budgeted briefing

**Phase:** v0.9 基础 / v0.10 完整
**Source:** SPEC §5；OI-004 §1.1/§1.2
**Priority:** high

## 目标
实现"同一抽象水平思考"：每个 Agent 看到的观察由其角色与当前
专注任务决定；Root 看全局与 KPI，Worker 看任务与细节。LLM 上下
文包含邮件正文、任务描述、相关知识，且受 token budget 约束。

## 要求 / 规则
- 定义 `ObservationPolicy`：sections、task_scope、kb_injection、
  max_tokens。
- 实现 `ContextCompiler.compile(agent, continuation, snapshot) ->
  AgentObservation`，替换当前"全量任务 + 全量 KB 路径"的同构观察。
- 默认策略：
  - root：mission、task_tree_summary、kpi_dashboard、
    escalations、pending_decisions；
  - manager：subtree 任务、子级状态、收件箱全文、相关 KB；
  - worker：focus task 详情、收件箱全文、工作区文件清单、
    相关 KB 条目。
- 邮件正文默认渲染；超过预算时摘要 + 引用（可先截断标记）。
- 工具定义从 ToolManifest 自动生成（与 v0.9-7 联动）。
- 任务观察按 `task_scope` 裁剪：`focus_task | owned | subtree | all`。

## 产出
- ObservationPolicy 模型与 ContextCompiler。
- 三套默认策略（root/manager/worker）。

## 验收标准
- [ ] root 观察不含 worker 私有文件内容，含任务树摘要与 KPI
- [ ] worker 观察含 focus task 全文与相关 KB，不含全量任务表
- [ ] 邮件正文出现在 LLM prompt 中（超限时截断且有标记）
- [ ] token budget 生效：编译结果不超过 max_tokens
- [ ] 新测试断言 root/manager/worker 观察形状差异
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
