---
kind: task
status: completed
phase: v0.14
source: 设计讨论（2026-08-27：统一身份模型 + 家目录布局）
priority: high
---

# 统一身份模型 + 家目录布局（FS 层落地）

## 内容

AgentOS 的身份（user）= **agent 或 device**，每个身份一个私密数据空间
（家）。**FS 层与 Authority 分离**：FS 层 = 挂载矩阵（静态、出生定格，
管"谁碰谁的盘"，不在 Authority 职责内）；Authority = 业务布线（谁能
调用谁，动态裁决）。由此"设备数据只经接口暴露"由 FS 隔离机械性强制：
agent 物理碰不到设备数据，只剩"调用 → 设备按 auth 裁决"一条路。

## 布局（约定即默认，零配置）

```
workdir/                        # 数据根（config 绑定）
  data/                         # 数据根容器（沙箱内挂载锚点）
    devices/<name>.py           # 设备源码（系统唯一识别区；agent 生产、设备只读加载）
    <identity>/                 # 各家（agent 与 device 同目录同形制）
```

- 设备源码是数据（一切皆数据），落数据根内、独立于任何家；agent 家里
  即使有 `devices/` 系统也不识别（唯一识别区 = `data/devices`）；
- identity 校验：拒 `/`、`..`、`.`，并**拒保留名 `devices`**（防源码区
  冲突与逃逸）；
- agent 家 = `workdir/data/<agent-id>`（注册时创建，幂等）；设备家 =
  安装者数据根的 `workdir/data/<device-id>`（装载时创建，幂等，沿用
  data-dir-convention）。

## 挂载矩阵（按身份类型，无 per-position 物化）

| 身份 | 家 `data/<id>` | 源码区 `data/devices` | 系统 |
|---|---|---|---|
| agent | 可写 | 可写（生产源码；**装载权在 Authority**，普通 agent 写了也装不了） | 只读 |
| device | 可写 | 只读（加载实现用） | 只读 |

agent 矩阵不含其它设备家（不可见）→ 设备数据只经接口暴露（调用层
auth 裁决，用例 2 语义不变）。

## 设备分界（实例化模式）

设备源码导出 `INSTANCE` 声明：`per-agent`（执行载体类：bash 等）或
`shared`（数据服务类：SharedKB 等），**必填无默认**。

- **per-agent**：安装时为每个绑定 agent 实例化一个进程，实例 identity =
  `<device-id>@<agent-id>`；挂载 = 绑定 agent 的家可写 + 源码区只读 +
  系统只读——**命令落 agent 家（用例 1：agent 经 bash 写自己家）**；
  工具条目注入该 agent，associated 指向实例 identity；
- **shared**：共享单实例，identity = `<device-id>`；挂载 = 自己家可写 +
  源码区只读 + 系统只读；业务按 auth 裁决（用例 2）。

## 技术要点

- `_device_workdir`（agent_os.py）：source_file 布局改为
  `<workdir>/data/devices/<name>.py`——校验中间两层恰为 `data/devices`，
  workdir = dirname ×3；
- bootstrap（agent.py）：扫描 `workdir/data/devices/*.py`；
- `_data_dir`/`_bwrap_args`（process.py / sandbox_entry.py）：写根按身份
  类型展开——agent：[家可写, 源码区可写]，device：[家可写, 源码区只读]；
  挂载参数含家与源码区两个锚点；沙箱判定（load_spec / workdir）不变；
- install_device 扩展：读取 INSTANCE 声明；per-agent 实例的 bound_agent
  传递；实例 identity 生成、注册、注入（条目只给绑定 agent）；
- SPEC 目录约定节重写 + 制造例外句修正（root agent 生产源码，落
  `data/devices`）；
- demo/check 脚本源码落盘路径迁移（`workdir/devices` → `workdir/data/devices`）。

## 验收

- workdir 根仅 `data/`；源码区 `workdir/data/devices`；agent 与 device 家同目录；
- agent 沙箱：家 + 源码区可写、其它设备家不可见、系统只读；
- device 沙箱：家可写、源码区只读、系统只读；
- per-agent 实例：bash 写绑定 agent 家成功、写其它家被拒（用例 1）；
- shared 设备按 auth 裁决不变（用例 2，回归）；
- identity 拒保留名 `devices`；全量回归 + lint + SPEC/卡一致。

## 依赖

data-dir-convention、sandbox-wrapper（已提交）；被 bash-sandbox-adapt
依赖（bash 参考实现声明 per-agent、cwd 落绑定 agent 家）。范围外：
/etc/passwd 式身份注册表数据文件（当前约定即默认）；SharedKB 类设备的
auth 裁决细节（富化已就绪，不改）。

## 完成

- 实现 `d65d36c`：布局（devices 进 `data/devices`、各家同目录）、挂载矩阵
  两锚点按身份类型（agent 家+源码区可写 / device 家可写+源码区只读）、
  INSTANCE 分界（per-agent 实例 `device@agent`、shared 单实例）、bootstrap
  与 `_device_workdir` 推导、SPEC/MEMORY 对齐。
- 审查修补（随收尾提交）：**data 根 tmpfs 掩蔽**（其它设备家真正不可见，
  兑现"物理碰不到设备数据"）；bootstrap 传 `bound_agent`（per-agent 经
  规范路径可装）；Authority 注入层 per-agent 隔离（卸载/全量重注入不向
  非绑定 agent 泄露）；identity 拒 `@`（设备名与实例命名空间不相交）；
  register 身份校验同口径前移；per-agent 卸载只重注入绑定 agent；
  SPEC 装卸节补"实例身份即装卸身份、换绑并存"。
- 冒烟：check_mount（矩阵掩蔽）、check_instance（per-agent 实例化/用例 1/
  换绑并存/卸载/注入隔离）全过；全量回归（demo + 8 check）PASS。
