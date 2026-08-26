---
kind: task
phase: v0.14
source: SPEC.md
priority: medium
---

# bash 设备沙箱语义适配 + PROTOCOL 更新

## 内容

bash 命令在沙箱内执行：cwd/工作目录与临时文件落数据区；输出文件落数据
区；禁网下网络命令行为明确；PROTOCOL.md 沙箱节重写（"setsid 逃逸进程组"
由 pid namespace（沙箱内 PID1 消亡整 ns 强杀）+ `--die-with-parent` 解决；四级门控与沙箱并存说明）。

## 验收

- bash 命令默认在数据区内执行（cwd=数据区），无法写出数据区；
- 禁网默认，声明后可用；
- PROTOCOL.md 与实现一致。

## 依赖

sandbox-wrapper、network-declaration（已提交）；被 sandbox-verification 依赖。
