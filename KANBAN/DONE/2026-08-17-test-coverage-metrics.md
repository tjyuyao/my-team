# Test Coverage Metrics

**Phase:** Infrastructure
**Source:** review #25; report §7 P3
**Priority:** P3 — Infrastructure

## 目标

添加测试覆盖率报告，覆盖行覆盖率和分支覆盖率。

## 要求

1. 安装 `pytest-cov`（已在 dev dependencies）
2. 运行 `uv run pytest --cov=src/my_team --cov-report=term-missing`
3. 目标：行覆盖率 ≥ 80%
4. 在报告中记录覆盖率数据
5. 识别关键路径（transaction commit, identity enforcement, lock acquisition）的覆盖情况

## 产出

- [ ] 覆盖率配置（`pyproject.toml` 中的 `[tool.coverage]`）
- [ ] 报告中记录覆盖率
- [ ] 识别未覆盖的关键路径

## 验收标准

- [ ] 行覆盖率 ≥ 80%
- [ ] 关键路径覆盖率 ≥ 70%
- [ ] 覆盖率数据在报告中可见
