---
kind: task
status: completed
phase: Documentation
source: "review #25"
priority: medium
---

# Acceptance Verification Commands


## 目标

在报告中提供可执行的验收命令和预期输出。

## 要求

提供以下命令的预期输出：

1. 最小 E2E 启动命令：
```bash
uv run python -c "
from my_team.simulation import Simulation
sim = Simulation.from_config_file('configs/sample-team.json')
results = sim.run(max_ticks=10)
print(f'Completed {len(results)} ticks')
for r in results:
    print(f'  tick={r.tick}: emails_delivered={len(r.emails_delivered)}, tasks_created={len(r.tasks_created)}')
"
```

2. 测试套件：
```bash
uv run pytest -q
```

3. 覆盖率：
```bash
uv run pytest --cov=src/my_team --cov-report=term-missing
```

4. 静态分析：
```bash
uv run mypy src/my_team
uv run ruff check src tests
```

## 产出

- [ ] 报告中添加 §2.5 "Verification Commands"
- [ ] 包含预期输出示例

## 验收标准

- [ ] 报告包含可执行的验证命令
- [ ] 命令与实际代码库一致
