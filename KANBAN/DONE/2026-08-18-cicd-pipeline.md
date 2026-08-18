---
kind: task
status: completed
phase: Infrastructure
source: "review #23; report §7 P3"
priority: low
---

# CI/CD Pipeline


## 目标

添加 GitHub Actions CI 流水线，覆盖多 Python 版本。

## 要求

1. GitHub Actions workflow：`test.yml`
2. Python 矩阵：3.10、3.11、3.12
3. 运行 `uv run pytest`
4. 运行 `uv run ruff check`
5. 运行 `uv run mypy`
6. 运行 `uv run pytest --cov`

## 产出

- [ ] `.github/workflows/test.yml`
- [ ] 验证 Python 3.10 兼容性
- [ ] 确认 Pydantic v2 在 3.10 上正常工作

## 验收标准

- [ ] CI 在 3 个 Python 版本上通过
- [ ] 测试、lint、类型检查均通过
