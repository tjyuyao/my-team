---
kind: task
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
  连坐互为冗余。

## 验收

- bash 命令默认在数据区内执行（cwd=数据区），无法写出数据区；
- 禁网默认，声明后可用；
- PROTOCOL.md 与实现一致。

## 依赖

sandbox-wrapper（已提交 b83bda6）、network-declaration（待实现，串行在前）；
被 sandbox-verification 依赖。
