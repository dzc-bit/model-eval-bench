# archive/ —— 各题成题脚本（历史存档，不可直接运行）

这里是 2026-09-30 成题期一次性使用的 `author_t0*.py`：从受测仓库读出原文、
生成注入补丁 / 参考解 / 隐藏测试 / p2p 白名单并写进 `packs/core/tasks/<ID>/`。

它们的产出已经全部入库（题包就是产物本身），此后不再参与任何流程：

- 修改题面 / 门禁 / 登记表用的是 `packs/core/tools/`（inject_edits、packcheck 等）
  与同目录的 `packgate.py`，不是这些脚本。
- 脚本顶部硬编码了当时的绝对路径（`D:\new model test` / `D:\New project 6`），
  且依赖 `import packgate`（同目录相对导入），移到本目录后**直接运行会失败**——
  这是有意的：它们是「这道题当时是怎么造出来的」的存档，不是维护工具。
- 真要参考某题的注入点与生成逻辑，看 `packs/core/tasks/<ID>/reference/notes.md`
  （每题都有逐条注入点表与门禁记录）；本文档级别的总账在仓库根 `README.md` §6。
- 彻底删除也安全：全部内容有 git 历史兜底。
