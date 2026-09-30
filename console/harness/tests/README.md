# harness 自测

后端自测。全部用例都跑在自造的迷你仓库与自造题包上，**不依赖 `packs\` 下由出题 agent
生产的真实题包**，也**不碰受测仓库 `D:\New project 6`**（只对配置里的路径做只读探测）。

## 跑法

```
cd D:\new model test
python -m pytest console\harness\tests -q
```

`pytest.ini` 已把 `--basetemp` 指到评测台内部，所有临时文件都落在 `D:\new model test` 内。

## 目录

```
console/harness/tests/
├─ conftest.py           公共装置：临时迷你仓库、临时题包、临时配置、模拟"模型改动"的工具
├─ pytest.ini            根目录收集设置（含 collect_ignore_glob，别把 fixture 里的测试收进来）
├─ fixtures/
│  ├─ mini_repo/         自造受测仓库：后端三处出口 + 前端一个面板 + docs/.reference/运行产物
│  └─ packs/core/tasks/  自造题包 TEST-01（后端 pytest）、TEST-02（前端 node 守卫，走联接）
└─ test_*.py             验收用例，见下
```

## 各文件在验什么

| 文件 | 验收点 |
| --- | --- |
| `test_snapshot.py` | 白名单快照、`docs/`/`.reference/`/`运行产物`/`node_modules` 不进沙箱、脱敏、裁剪、泄漏兜底 |
| `test_sandbox.py` | 准备/清空/重建、注入补丁顺序、subst 映射、`.gitignore` 保护行、node_modules 联接全程完好 |
| `test_grade.py` | 全链路分组部分分（16.7 / 33.3 / 100）、p2p 回归判 0、越界与作弊记录、评分树隔离 |
| `test_api.py` | `/api/health`、`/api/tasks`（空题包返回空列表）、缺失静态文件的中文 404 |
| `test_scoreboard.py` | 记��与统计：Wilson 区间、CSV、revealed 单独成块 |
| `test_selfcheck.py` | 静态自检：真代码里的危险命令能被抓出来，前端 js 规则能跑通 |
