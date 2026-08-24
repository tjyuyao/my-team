# tests/integration — 集成测试层

本目录存放**慢/重/墙钟依赖/真实进程**的集成测试（e2e 多 tick、持久化
roundtrip、真实子进程沙箱、HTTP Control Plane、LLM Dispatcher、日历真实
时钟、超时语义、崩溃防护、看板门禁）。

## 分层约定（v0.10.0 收尾，测试基建）

- `tests/` 根目录 = **单元层**（快，核心回归；开发迭代冒烟跑这一层）
- `tests/integration/` = **集成层**（全量验证/发布前必跑）

## 运行命令

```bash
uv run pytest -q tests/ --ignore=tests/integration   # 单元层冒烟（~1 分钟）
uv run pytest -q tests/integration/                  # 集成层（~45 秒）
uv run pytest -q                                     # 全量（~1 分 40 秒）
```

移动规则：新增测试若属 e2e / 真实进程 / 墙钟等待 / 长超时语义，
放本目录；纯单元逻辑（无 Simulation 全流程、毫秒级）放 `tests/` 根。

> 背景：全量 1006 tests 约 101s，其中 20 个集成文件（170 tests）占
> 44s。分层后开发迭代不必每次跑全量；全量保留给 CI 与发布前验证。
> 不引入 pytest-xdist 并行（sandbox 测试 RLIMIT_NPROC 用户级计数在
> 多 worker 下有互相干扰风险，暂缓）。
