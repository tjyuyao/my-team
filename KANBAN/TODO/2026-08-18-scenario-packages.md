---
kind: task
phase: v0.11 场景
source: SPEC §11；OI-004 §3、OI-005/006
priority: medium
---

# v0.11-13: 场景包系统与首个场景包 demo


## 目标
五个目标场景均通过"场景包"安装运行；内核代码不因场景变化而修改。

## 要求 / 规则
- 场景包结构：scenario.json、org_tree.json、roles.json、tools/、
  record_schemas/、ingress_adapters/、schedules.json、
  approval_policies.json、kb_seed/、kpi_dashboards/。
- 加载即校验：工具 manifest、记录 schema、审批策略、组织树无环、
  WorkerPool 存在；非法项拒绝加载并给出结构化错误。
- 首个 demo 先做软件开发公司场景包：需求 → 拆解 → 实现 →
  apply_patch → run_tests → 交付 的端到端流程。
- 后续场景包：小说工作室、电商、自媒体、知识星球。

## 产出
- 场景包加载器与校验器。
- 至少一个可运行场景包 demo。

## 验收标准
- [ ] 从配置加载场景包，内核代码不变
- [ ] 非法场景包被拒绝并给出结构化错误
- [ ] 软件公司场景包端到端 demo 可运行
- [ ] 其余四个场景包有配置骨架与验收清单
- [ ] `uv run pytest -q` 全绿；`ruff`/`mypy` 通过
