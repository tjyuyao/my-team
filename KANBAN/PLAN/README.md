# PLAN 目录

存放**版本计划**（多任务、跨版本的规划文档）。活跃与已归档的计划都
留在本目录。

- 计划不是任务：计划描述"要达成什么、由哪些任务组成、依赖与验收门"。
- 计划拆出的原子任务放入 `../TODO/`，并在计划中列出 TODO 文件名。
- **计划完成**（其引用的 TODO 全在 DONE 或有显式否决）后，留在本目录
  原地改名为 `{date}-{topic}.archived.md`，并在 frontmatter 标注
  `status: archived`。
- 版本发布后另写版本报告到 `../MILESTONE/`；**一旦有对应 MILESTONE
  报告，该版本计划必须归档**（不得保持 active，校验器 R10 强制）。
- frontmatter：`kind: plan` 必填；活跃计划 `status: active`，归档计划
  `status: archived`。
- 文件名仍遵守 `{YYYY-MM-DD}-{topic}.md`（日期 = 最后修改日）。