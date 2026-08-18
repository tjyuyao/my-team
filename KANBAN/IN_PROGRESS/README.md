# IN_PROGRESS 目录

存放**正在执行的任务**（kind=task）。规则：

- 开始一个 TODO 时，**必须**先把文件从 `../TODO/` 移到这里，并将
  文件名日期更新为当天（日期 = 文件最后修改日，由
  `../enforce_filename_dates.py` 强制）。
- 同一时间本目录文件数不得超过 WIP 上限（默认 3）。
- 任务完成：移入 `../DONE/`，更新日期，frontmatter 标
  `status: completed`（否决则 `rejected`）。
- 任务阻塞/需要重新计划：移回 `../TODO/`，更新日期。
- 本目录不允许存放 plan 或 issue，只存放任务文件；
  frontmatter `kind: task` 必填。