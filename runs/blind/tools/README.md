# runs/blind/tools/ —— 盲测与门禁工具

> `archive/` 里的 `author_t0*.py` 是成题期一次性脚本，**不能直接运行**（见 `archive/README.md`）。
> 本文只讲可用的那几个。

## 一、门禁（出题侧自验）

```bash
python runs/blind/tools/packgate.py --task T3-09 --state fixed      --out ..\tasks\T3-09\calibration\gate_fixed.json
python runs/blind/tools/packgate.py --task T3-09 --state partial    --out ...\gate_partial.json
python runs/blind/tools/packgate.py --task T3-09 --state injected --repeat 20 --out ...\gate_injected_x20.json
python runs/blind/tools/packgate.py --task T3-09 --state baseline   --out ...\gate_baseline.json
```

四态缺一不可：`fixed`=100、`partial`<100、`injected`×20 稳定 0、`baseline` 留档。
含 vitest 的题（前端/组合题）只能用 `packgate.py`；纯 pytest 题也可以用
`packs/core/tools/selfgrade.py`（它遇到 vitest 会 fail-closed）。

`--state custom --patch <补丁>` 用来给**任意**模型补丁打分（盲测用这条）。

## 二、盲测（拿 pass@1 用，非出题者本人执行）

三步，对应三个可复用工具：

```bash
# 1) 建注入态沙箱（= 被测模型该看到的那棵树；自动借用 packgate 的建树逻辑）
python runs/blind/tools/blindtool.py build T3-09 D:\tmp\blind\T3-09\work

# 2) 复制一份干净作答沙箱，然后按生产口径跑一轮作答
python runs/blind/tools/blindtool.py prep D:\tmp\blind\T3-09\work D:\tmp\blind\T3-09\trial-1
python runs/blind/tools/blindprobe.py --task T3-09 \
    --work D:\tmp\blind\T3-09\trial-1 --out D:\tmp\blind\results\T3-09-trial1.json \
    --prompt-file D:\tmp\blind\frozen\T3-09-1.md

# 3) 抽补丁（含越界判定）并按生产评分树打分
python runs/blind/tools/blindtool.py diff T3-09 D:\tmp\blind\T3-09\work D:\tmp\blind\T3-09\trial-1 D:\tmp\blind\T3-09\trial-1.patch
python runs/blind/tools/packgate.py --task T3-09 --state custom --patch D:\tmp\blind\T3-09\trial-1.patch --out D:\tmp\blind\grades\T3-09-trial1.json
```

### 为什么探针必须复用生产代码

`blindprobe.py` 直接 `import harness.chat`，复用生产的 `TOOLS`/`TOOL_HANDLERS`、
`_system_prompt`（含 `allowed_paths` 披露）与工具结果序列化逻辑。
这不是洁癖，是实测教训：同一道 T3-09、同一个模型，自造工具（缺 `run_command`，
模型无法自己跑 pytest）测出 **14.29**，复用生产工具层测出 **71.43**。
自造探针会系统性高估难度，据此调档会把题改坏。

### 四条会让数据作废的坑（都踩过）

1. **同题并发**：两个进程同时跑同一道题时 `trial-<n>` 目录同名，后启动的 `prep`
   会把前一个正在用的沙箱删掉——模型会在只剩 `node_modules` 的空目录里跑几十步，
   补丁为空。`blindtool.py prep` 因此有完整性闸门（<100 个文件直接报错），
   编排侧还要加独占锁。
2. **题面中途被改**：探针是每轮现读题面的，盲测期间改动题库会让同一组各轮读到不同题面，
   聚合出的 pass@1 没有意义。开跑前先把题面冻结成文件并用 `--prompt-file` 指向它；
   结果 JSON 里的 `prompt_sha256_16` 就是事后逐轮核对同源的凭据。
3. **补丁格式**：目标仓库的最小 diff 应用器用 `diff --git` 当文件分隔符、
   `+++ b/<path>` 取目标路径。只用 `difflib.unified_diff` 的默认空文件名会把路径冲成空串，
   报 `FileNotFoundError: 补丁目标不存在`。必须同时给 `diff --git a/X b/X` 与带名的 `+++`/`---`
   （`blindtool.py diff` 已处理）。
4. **越界判定口径**：`allowed_paths` 之外的改动记违规、命中即整轮判红；
   但 `util.ALWAYS_SKIP_DIRS`（含 `.ruff_cache`、`.mypy_cache`、`.vite`）根本不进全树清单，
   运行产物按 `grade.NOISE_GLOBS` 只提示。探针少查任何一条，分数都会偏高。

### 数据纪律

- 样本量小的时候（n≤2）**不要**下 `target_band` 结论；同题同模型实测波动可以很大
  （T1-01 一轮 100、一轮 0）。
- 探针带保守偏差时（如自设 `max_tokens`、截断工具输出）测得的分是**下界**，
  要在记录里写明。
- `calibrated` 保持 `false`，`blind_runs.rows` 只写真实跑出来的行，不填造、不外推。
