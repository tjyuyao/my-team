---
kind: task
status: completed
phase: v0.14
source: SPEC.md
priority: medium
---

# bash 设备沙箱语义适配 + PROTOCOL 更新

## 内容

bash 命令在沙箱内执行（设备进程已在 bwrap 固定矩阵内，其 Popen 的子
命令继承同一沙箱）：cwd/工作目录与临时文件落数据区；输出文件落数据区；
禁网下网络命令行为明确；PROTOCOL.md 沙箱节重写（"setsid 逃逸进程组"
由 pid namespace（沙箱内 PID1 消亡整 ns 强杀）+ `--die-with-parent`
解决；四级门控与沙箱并存说明）。

## 技术要点

- **data_dir 到设备的机制（方向已定）**：内核在 spawn 设备进程时经
  环境变量注入数据区路径（如 `MY_TEAM_DATA_DIR`，约定即默认、零配置）——
  设备（bash 设备参考实现）从 os.environ 读取后作为默认 cwd/输出根；
  环境变量随 bash 子命令继承（设备进程内本就可读，可接受）；
- 沙箱判定：bash 设备经 install_device 装载（load_spec 非空）→ 默认
  进沙箱；needs_network 声明（network-declaration 卡完成后）决定是否
  保留网络面；
- 四级门控（timeout/deadline/max_deadline/设备终止连坐进程组）与沙箱
  pidns 强杀语义并存：设备终止 → 沙箱 PID1 消亡 → 整 ns 强杀，与进程组
  连坐互为冗余；
- **/dev 字符设备不可写（矩阵已知后果，本卡承接）**：`--ro-bind / /` 下
  沙箱内 `>/dev/null` 等 open-for-write 被拒（Permission denied）；bash
  设备自身 DEVNULL 为读打开不受影响，但用户命令会踩中——本卡决定选择性
  rw 绑定（如 `--bind /dev/null /dev/null`）或文档化限制。

## 验收

- bash 命令默认在数据区内执行（cwd=数据区），无法写出数据区；
- 禁网默认，声明后可用；
- PROTOCOL.md 与实现一致。

## 依赖

sandbox-wrapper（已提交 b83bda6）、network-declaration（待实现，串行在前）；
被 sandbox-verification 依赖。

## 完成

- 实现：矩阵加 `--dev /dev`（devtmpfs 覆盖 /dev，`>/dev/null` 与
  `open('/dev/null','r+')` 可用；实测 `--bind /dev/null` 无效，`--dev`
  有效且须置于 `--ro-bind / /` 之后）；`_sandbox_reexec` env 注入
  `MY_TEAM_DATA_DIR = writable[0]`（各类身份均为该进程数据家）；
  bash 设备 `default_cwd = os.environ.get(MY_TEAM_DATA_DIR)`，`bash_run`
  不指定 cwd 时默认落数据家（沙箱外直接实例化行为不变）；PROTOCOL.md
  增"沙箱语义"节（setsid 逃逸由 pidns + die-with-parent 收敛，四级门控
  与沙箱强杀互为冗余）。
- 审查：PASS-with-nits（主 agent + 独立 subagent 双审）；修补随收尾
  （恢复 PROTOCOL.md `## 待定项` 节 watch/bash_list/remind 条目、文件
  末尾空白、测试 [4] 改用帧 ok 字段断言、补 [5] 跨家掩蔽验证）。
- 故事测试 `tmp/check_bash_sandbox.py`：默认 cwd=数据家、/dev/null 可写、
  写不出数据区（系统+源码区）、禁网、跨家不可见——全过；全量回归 & lint 0。
