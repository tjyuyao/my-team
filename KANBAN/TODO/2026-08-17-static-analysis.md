# Static Analysis (mypy + ruff)

**Phase:** Infrastructure
**Source:** review #23; report §7 P3
**Priority:** P3 — Infrastructure

## 目标

添加 mypy 类型检查和 ruff 代码质量检查。

## 要求

1. 添加 `mypy` 到 dev dependencies
2. 添加 `ruff` 到 dev dependencies
3. 配置 `pyproject.toml` 中的 `[tool.mypy]` 和 `[tool.ruff]`
4. 修复所有 mypy 错误
5. 修复所有 ruff 警告

## 产出

- [ ] `pyproject.toml` 中的 mypy/ruff 配置
- [ ] 修复所有类型错误
- [ ] 修复所有 lint 警告
- [ ] 在 CI 中集成

## 验收标准

- [ ] `mypy src/my_team` 无错误
- [ ] `ruff check src tests` 无警告
