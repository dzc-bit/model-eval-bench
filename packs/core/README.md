# packs/core —— 任务包出题指南（§8 的可执行落地）

本目录是评测台的任务包：**题库与答案的家，永不进沙箱**（§4.2 第 1 层：沙箱
白名单快照只含 `backend/ tests/ scripts/ pyproject.toml .gitignore` 等运行
必需文件；`packs/` 永不在名单上）。

- 受测仓库：**只读**。本目录的一切产出都以"从仓库复制出来再改"的方式生成，
  全流程结束后必须满足 `git -C <仓库> status` 无新增条目。
- 通用校验引擎在 `console/harness/`（另一条工作线）；本目录只提供**出题侧
  自验工具**（`tools/`），它不追求与正式 harness 实现一致，也不是第二套
  harness——只是把"这道题能不能入库"这件事在入库前先验一遍。

---

## 一、目录结构（§8 定案）

```
packs/core/
├─ index.json                     ← 包清单（题目登记表，供任务库页面与 harness 读取）
├─ README.md                      ← 本文件
├─ tools/                         ← 出题侧自验工具（纯标准库）
│  ├─ mkpatch.py                  ← unified diff 生成器
│  ├─ inject_edits.py             ← 注入规格（可审计的精确字符串替换）
│  ├─ solution_edits.py           ← 参考解规格（fix / partial，相对注入态）
│  ├─ selfgrade.py                ← §5.3 门禁自验（拼评分树、跑 pytest、按组算分）
│  ├─ packcheck.py                ← 完整性 + §6.5 清单 + 提示词名词泄露扫描
│  └─ _work/                      ← 草稿与探针产物（非交付物，可整目录删除）
└─ tasks/<题目ID>/
   ├─ meta.json                   ← schema 1 元数据（字段对齐 §8）
   ├─ prompts/1.md 2.md 3.md      ← 三级提示词（附录 A；永不出现文件/函数/常量名）
   ├─ inject/patches/0001-*.patch ← 合成注入补丁（unified diff，git apply 可用）
   ├─ hidden/tests_hidden/*.py    ← 隐藏测试（评分时才出现）
   ├─ hidden/groups.json          ← 分组与权重（组 = 一个出口；p2p 组权重 0）
   ├─ p2p.json                    ← 必须保持绿的既有用例白名单
   ├─ reference/fix.patch         ← 锚解（相对注入态）
   ├─ reference/partial.patch     ← 半成品解（只修一个端口，演示 <100）
   ├─ reference/notes.md          ← 出题侧存档：注入点、陷阱、§6.5 打勾、门禁结果
   └─ calibration/                ← results.json（校准总表）+ gate_*.json（门禁原始输出）
```

草稿题（`status: "draft"`）只要求 `meta.json` + `prompts/1.md` +
`reference/notes.md`（症状草案 + 注入点清单），packcheck 按草稿级检查。

---

## 二、答案隔离红线（每一步都要回头对照）

1. `reference/` 与 `hidden/` **永不出现在沙箱快照白名单**；`allowed_paths`
   不得指向这两处。
2. 症状与三级提示词**不得出现任何文件名 / 函数名 / 常量名**——只用业务词汇。
   这条只约束**题面**。可改文件边界（`allowed_paths`）由 harness 的系统提示词逐轮告知模型
   （见 `console/harness/chat.py:_system_prompt`），不属于题目信息量；
   越界会按路径作废整轮，所以这条规则**必须**让模型知道，否则惩罚不成立。
   `packcheck.py` 会把 allowed_paths、隐藏测试、注入补丁、参考解里的标识符
   汇成禁用词表，逐级扫描 `prompts/*.md`。
3. 题目 ID 不含答案词（`T1-01`、`T2-04` 这种编号即可）。
4. 注入必须是**合成改写**，与历史修复 diff 相似度 < 0.6（§4.2 第 3 层）：
   不做"把历史修复反向打回去"的事，改写注释措辞、引入历史上不存在的
   形态（例如新导入、被拆散的原子操作）。
5. 脱敏：`meta.redactions` 删除 AGENTS/CHANGELOG 中直接描述该缺陷的段落；
   `meta.visible.prune` 移除名字或断言直接点名的守卫测试。**0 个可见红测试**
   是入库条件（被注入连带打红的可见测试也要裁掉，并从 p2p 同步剔除）。
6. 隐藏用例**不得断言只有锚解才有的名字**。凡是"新起的对外字段名 / 函数名 / 类名"
   （锚解引入、三级提示词里 0 次出现），断言必须写成**形态检查**：值对不对、能不能查到，
   而不是"叫什么"。否则评分考的是猜锚解的命名，不是设计与修复。
   判别力不受影响的判断方法：注入态的产物里也不得出现那个信息（如 409 的在途任务编号，
   基线文案只报数量不报编号，则"编号可查"仍然是红）。

---

## 三、出题流水线（逐步可执行）

### 步骤 0 · 勘察（仓库只读）

通读受测仓库相关模块与守卫测试，产出两份名单：
**真实缺陷模式**（§7 十一道题对应的注入面）与**点名答案的守卫测试**（prune
候选）。把结论写进 `reference/notes.md` 第一节。

### 步骤 1 · 定档（§6.3 硬性规格）

| 档 | 尝试 | 规格 | 目标带（强模型 pass@1） |
| --- | --- | --- | --- |
| 初级 primary | 1 | ≥3 文件、≥1 静默注入、0 可见红测试、轻度脱敏；修复 = 抽共享点接通 ≥2 调用方 | 0.60~0.85 |
| 中级 medium | 2 | ≥3 模块（含 ≥1 消费方/前端）、≥2 静默、中强度脱敏、≥1 陷阱；修复 = 设计机制或协议 | 0.25~0.55 |
| 高级 hard | 3 | ≥4 模块（尽量跨语言）、必须自行设计机制、≥2 陷阱、全脱敏、≥5 隐藏组、并发/性能硬约束 | 0.05~0.25 |
| 王者 king | 3 | ≥6 可改文件、≥8 个计分组；至少四类跨模块不变量共同约束，含并发/时序与性能硬约束，另有联合 coherence 组 | pass@3 目标 0.00~0.02，必须盲测验证 |

王者仍是三轮机会，`attempts` 必须为 3。`target_metric: "pass_at_3"` 是设计目标，不是作者可自证的承诺；在非出题模型盲测前 `calibrated` 必须为 `false`。确定性隐藏测试可以证明参考解可通过、注入态不可通过、半成品不满分，但不能证明所有能力等级的模型三轮必败。

建 `tasks/<ID>/`，先写 `meta.json`（status: draft）与 `prompts/1.md` 症状草案。

### 步骤 2 · 注入（合成改写）

1. 在 `tools/inject_edits.py` 增补本题的编辑规格：每条注入是
   `(注入后文本, 原文锚点)` 的精确字符串替换；`materialize()` 会验证每个
   锚点在原文件中**恰好命中一次**，失配即炸。
2. 生成并落盘补丁：

   ```
   cd packs/core/tools
   python inject_edits.py --repo "D:\New project 6" --task T1-01 --work-dir _work
   # 产物：_work/<ID>/patches/0001-*.patch …（diff -u 语义，git apply 可用）
   ```

3. 把补丁复制进 `tasks/<ID>/inject/patches/`，文件名 `NNNN-<模块>.patch`。
4. 红线自查：注入不改函数签名、不抛新异常；与历史修复的形态不同
   （改写注释、引入历史没有的导入/结构）。

### 步骤 3 · 隐藏测试与分组

- `hidden/tests_hidden/test_<主题>.py`：每个组至少**两个独立数据场景**
  （§6.5 第 3 条：硬编码/特判必挂）；fixture 数据用临时目录自建，不读仓库
  真实数据。
- `hidden/groups.json`：组 = 一个出口/端口；`coherence` 组权重最高（≥2），
  只断言"多出口对同一事实口径一致"；`p2p` 组 `weight: 0, mode: regression`。
- 附录 B 的四出口结构是缺口一致性题的参照（coverage / per_symbol / sync_pool
  / flow / coherence / p2p）。

### 步骤 4 · p2p 白名单

基线（未注入）全绿的既有用例名单。生成办法：注入态下全量跑一遍仓库测试，
把**注入导致变红**的用例与**点名答案**的用例裁掉（进 `visible.prune`），
剩下的就是 p2p。快照里基线本来就红的用例（与本任务无关的）也要剔除。
p2p 断裂 → 本轮作废 0 分，所以宁缺毋滥。

### 步骤 5 · 三级提示词（附录 A）

- **接线说明**（每级固定开头）：

  ```
  你面前有一个独立的代码仓库副本，工作目录是服务端提供的沙箱文件夹路径，
  它是唯一允许操作的位置，不要访问该目录之外的任何路径。
  请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。
  ```

  （盘符池已退役，不要再写「当前目录 / Q:\」；沙箱是 `sandbox_root` 下的普通文件夹，
  具体路径由工作台服务端提供，`packcheck.py` 按上面这三句固定措辞校验。）

- **第 1 级（症状）**：纯用户视角，一两段现象 + 验收要求；无文件名/函数名。
- **第 2 级（不一致清单）**：指出"同一事实被几处各自计算、现在互相矛盾"，
  仍不定位到函数。信息量必须比第 1 级多（packcheck 校验字数递增）。
- **第 3 级（不变量 + 否决项）**：给出必须同时成立的不变量表述 +
  1~3 条"已被否决的思路及原因"（防重提），仍不给 diff。
- 三级内容必须不同、逐级增加信息量；同一级对同一任务固定不变。

### 步骤 6 · 参考解

1. 在 `tools/solution_edits.py` 增补本题的 `fix` 与 `partial` 规格
   （都相对**注入态**；未注入的解题文件以仓库原文为 diff 基线）：

   ```
   python solution_edits.py --repo "D:\New project 6" --task T1-01 --work-dir _work
   # 产物：_work/<ID>/solution_fix.patch / solution_partial.patch
   ```

2. `fix` 必须让全部目标组绿且不触碰 `forbidden_paths`；`partial` 只修
   一个端口，演示"必然 <100"。
3. `reference/notes.md` 写全：锚解形态、注入点表、陷阱（含诱饵点）、
   §6.5 六条逐条打勾、`visible.prune` 每条的理由、门禁结果表、校准状态。

### 步骤 7 · 门禁自验（§5.3，输出进 calibration/）

```
cd packs/core/tools
python selfgrade.py --task T1-01 --repo "D:\New project 6" --state fixed   --timeout 900 --out ..\tasks\T1-01\calibration\gate_fixed.json
python selfgrade.py --task T1-01 --repo "D:\New project 6" --state partial --timeout 900 --out ..\tasks\T1-01\calibration\gate_partial.json
python selfgrade.py --task T1-01 --repo "D:\New project 6" --state injected --repeat 20 --timeout 900 --out ..\tasks\T1-01\calibration\gate_injected_x20.json
```

入库门槛：

| 门禁 | 标准 |
| --- | --- |
| 锚解 fixed | 100/100；目标组全绿；不触碰 forbidden_paths；p2p 全绿 |
| 半成品 partial | < 100（只修一个端口必然不满分） |
| 注入态 ×20 | 全部 0 分、目标组全红、稳定不 flaky、p2p 零断裂 |
| 基线 | p2p 白名单在未注入快照上全绿（p2p 的定义即来自这一步） |

### 步骤 8 · 入库

```
python packcheck.py --repo "D:\New project 6"
# 全部红项清零后：
#   1) meta.json 删除 "status": "draft" 与所有 "待定" 占位
#   2) 在 packs/core/index.json 登记本题
```

---

## 四、校准纪律（§6.4，硬规则）

**出题者不做盲测校准。** `calibration/results.json` 在盲测完成前保持空表
（`blind_runs.rows: []`，`summary` 全 null），`calibrated` 恒为 `false`，
meta 与 results 两处一致。盲测由用户在后续组织的**非出题模型**实例上完成；T1–T3
每行记录一个只给第 1 级提示词的独立作答（pass@1），王者每行记录一个从第 1 级开始、
同一沙箱内逐轮校验并最多解锁三级提示的完整会话（pass@3，另记 `rounds_used`）。
王者同一会话的提示轮不是独立样本。结果需记录 Wilson 95% 置信区间；弱模型测下界、
强模型测上界。packcheck 会校验指标字段并拦截未校准时填写的 summary。

---

## 五、tools/ 命令速查

| 目的 | 命令 |
| --- | --- |
| 生成注入补丁 | `python inject_edits.py --repo <仓库> --task <ID> --work-dir _work` |
| 生成参考解补丁 | `python solution_edits.py --repo <仓库> --task <ID> --work-dir _work` |
| 单独造 diff | `python mkpatch.py --pair "backend/x.py:<原>::<改>:" --out-dir _work` |
| 门禁自验 | `python selfgrade.py --task <ID> --repo <仓库> --state <fixed\|partial\|injected\|baseline> [--repeat 20] --out <json>` |
| 完整性自检 | `python packcheck.py --repo <仓库> [--task <ID>] [--out _work\packcheck.json]` |

约定：

- `selfgrade.py` 自带 unified-diff 应用器与测试裁剪器，**只服务出题侧自验**；
  它不是正式 harness 的实现，也不应被 console 引用。
- 自验产生的评分树、探针、临时 JSON 全部放 `tools/_work/`，不入交付物；
  `_work` 可整目录删除后重跑。
- 所有脚本纯标准库（Python 3.13），不依赖仓库虚拟环境；跑 pytest 时直接用
  系统 `python -m pytest`（仓库 `pyproject.toml` 自带 `pythonpath=["backend"]`）。

---

## 六、Windows 删除安全（§4.3 摘录，出题脚本同样受约束）

1. 永不 `del /s`——它会穿透 junction 删除真实 `node_modules` 内容。
2. 永不 `git clean -fdx`（沙箱内清空用 `git clean -fd`，不带 `-x`）。
3. 删整棵临时树用 `shutil.rmtree(<树根>)`（Python 3.13 不跟进 junction）；
   单独移除联接用 `os.rmdir`。
4. 对 junction 本身跑 `shutil.rmtree` 会抛 `OSError`——先 `os.rmdir` 联接。

---

## 七、index.json

包清单，一题一行登记：`id / tier / status / attempts / title / target_band /
prune_count / p2p_count` 等只读摘要。任务库页面与 harness 读它来列题；
**答案细节（allowed_paths、注入点）不进 index**，留在各题 meta 与 reference 里。
