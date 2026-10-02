"""harness 自测的公共装置。

四条纪律：
1. 所有读写都落在 D:\\new model test 内——连 pytest 的临时目录也指到评测台里
   （见 pytest_configure），绝不写到系统盘；
2. 受测仓库 D:\\New project 6 全程只读，测试只拿 fixtures/mini_repo 这份自造迷你仓库；
3. 夹具里的题包是自造的，不依赖 packs\\ 下由出题 agent 生产的真实题包；
4. 每个用例的工作区都落在 pytest 临时根目录内；用例结束只清理实体目录，
   不创建或回收任何盘符映射。
"""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

# fixtures/ 里放的是"被测代码"（迷你仓库的 tests、题包的 hidden），
# 不是本次要跑的用例，双保险：这里再挡一次。
collect_ignore_glob = ["fixtures/*"]

# 让测试能直接 import harness.*（不必先把 console/ 装进 sys.path）
CONSOLE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVAL_ROOT = os.path.dirname(CONSOLE_DIR)
if CONSOLE_DIR not in sys.path:
    sys.path.insert(0, CONSOLE_DIR)

from harness import config as harness_config          # noqa: E402
from harness import sandbox, util                    # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
MINI_REPO = os.path.join(FIXTURES, "mini_repo")
FIXTURE_PACKS = os.path.join(FIXTURES, "packs")

#: pytest 的临时工作区根，固定落在评测台内（硬约束：一切读写都在 D:\\new model test 内）
#: 可用环境变量 EVAL_PYTEST_TMP 覆盖；默认目录不可写（历史上被残留句柄锁住过）时
#: 自动退到同根下的 .pytest-tmp-<pid>，不让整个测试套因为清理残留而集体红。
def _pick_pytest_tmp() -> str:
    override = os.environ.get("EVAL_PYTEST_TMP")
    if override:
        return override
    default = os.path.join(EVAL_ROOT, ".pytest-tmp")
    try:
        os.makedirs(default, exist_ok=True)
        probe = os.path.join(default, ".writable-probe")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return default
    except OSError:
        return os.path.join(EVAL_ROOT, ".pytest-tmp-%d" % os.getpid())


PYTEST_TMP = _pick_pytest_tmp()

#: 迷你仓库里那几个绝不该进快照的顶层目录（验收要逐个点名它们不在）
NEVER_SNAPSHOTTED = ["docs", ".reference", "运行产物", "node_modules"]
#: 夹具题号
BACKEND_TASK = "TEST-01"
FRONTEND_TASK = "TEST-02"


def pytest_configure(config):
    """把 basetemp 钉死在评测台内。

    命令行给的相对 --basetemp 是相对「当前工作目录」解析的，从别的目录
    跑 pytest 就会写穿到系统盘。这里无条件改成绝对路径，落在评测台里。
    """
    config.option.basetemp = PYTEST_TMP


@pytest.fixture(scope="session", autouse=True)
def sweep_stale_workspaces():
    """开跑前后不触碰宿主机盘符，只让测试工作区保持在临时根内。"""
    yield


@pytest.fixture(autouse=True)
def reclaim_workspaces():
    """每个用例的实体工作区由 pytest 临时目录统一回收。"""
    yield


@pytest.fixture(autouse=True)
def forbid_writing_real_config(monkeypatch):
    """保险丝：测试绝不允许写真实的 config.json、密钥文件、runs/ 与 sandboxes/。

    起因（2026-10-01）：模型配置重构后，几个走 HTTP 的用例只 patch 了「读配置」
    的入口，写路径仍然指向真实 console/config.json —— 一次全量测试把用户的
    3 个模型档案替换成了测试数据。写路径必须默认被挡住，用例要写就自己
    patch 到 tmp_path，而不是靠每个用例自觉。

    2026-10-02 追加 runs/ 与 sandboxes/ 的同款保险丝。起因是一次真实数据事故：
    ``test_provider_migration`` 只 patch 了 ``config.CONFIG_PATH``，没有覆盖
    ``runs_root``/``sandbox_root``，于是 ``config.load()`` 把它们解析到真实的
    ``runs/`` 与 ``sandboxes/``；该用例又调用 ``runs.delete_provider(cfg, "local-20128")``。
    此前 ``delete_provider`` 的 ``with_runs`` 默认为 False，它只在密钥上生效，
    属于**侥幸安全**；一旦级联变成默认行为（删档案即彻底删除），同一个用例就
    真删了本机在册的四条运行记录（含对话、报告、diff 与沙箱）。

    教训比这条断言更重要：**「读侧 patch 了配置」不等于「写侧落在临时目录」**。
    凡是会删除或覆写的路径，一律在这里默认被挡住。
    """
    from harness import config as harness_config
    from harness import keyring as harness_keyring

    real_config = os.path.abspath(harness_config.CONFIG_PATH)
    real_keys = os.path.abspath(harness_keyring.path())
    real_runs = os.path.abspath(os.path.join(EVAL_ROOT, "runs"))
    real_sandboxes = os.path.abspath(os.path.join(EVAL_ROOT, "sandboxes"))

    def _is_under(child, parent):
        child = os.path.normpath(child)
        parent = os.path.normpath(parent)
        return child == parent or child.startswith(parent + os.sep)

    def _guard(path, kind):
        target = os.path.abspath(str(path))
        if target in (real_config, real_keys):
            raise AssertionError(
                "测试试图写真实%s（%s）。把写路径 patch 到 tmp_path 再跑。" % (kind, target))
        for root, name in ((real_runs, "runs/"), (real_sandboxes, "sandboxes/")):
            if _is_under(target, root):
                raise AssertionError(
                    "测试试图写真实的%s（%s）。该路径必须落在 EVAL_PYTEST_TMP 之下——"
                    "只 patch config.CONFIG_PATH 是不够的，runs_root / sandbox_root "
                    "要一起覆盖，否则删除类用例会真删本机的运行记录。"
                    % (name, target))

    for name in ("save", "update_models", "update_providers"):
        original = getattr(harness_config, name)
        monkeypatch.setattr(
            harness_config, name,
            (lambda orig: lambda *a, **kw: _guard(
                harness_config.CONFIG_PATH, "config.json") or orig(*a, **kw))(original),
            raising=False)
    for name in ("set_key", "remove_key", "rename_key"):
        original = getattr(harness_keyring, name)
        monkeypatch.setattr(
            harness_keyring, name,
            (lambda orig: lambda *a, **kw: _guard(
                harness_keyring.path(), "密钥文件") or orig(*a, **kw))(original),
            raising=False)

    # 删除类操作（级联删档案 / 真删记录 / 清沙箱）拿到的路径来自
    # cfg["runs_root"] / cfg["sandbox_root"]。检查必须放在**入口**而不是 purge_run：
    # 级联删是先 list_runs 再逐条 purge 的，真实 runs/ 空了的话 purge 一次都不会被调到，
    # 挂在 purge 上的检查就成了摆设——那正是它第一次失效的方式。
    from harness import runs as harness_runs
    from harness import sandbox as harness_sandbox

    def _check_roots(cfg, why):
        for key, name, root in (("runs_root", "runs/", real_runs),
                                ("sandbox_root", "sandboxes/", real_sandboxes)):
            value = str((cfg or {}).get(key) or "")
            if value and _is_under(os.path.abspath(value), root):
                raise AssertionError(
                    "用例%s让 cfg[%s] 指回真实的%s（%s）。删除是不可逆的，"
                    "每个删除类用例都必须把 runs_root 与 sandbox_root 一起指到 tmp_path。"
                    % (why, key, name, value))

    def _guard_callable(orig, why):
        def _wrapped(cfg, *args, **kwargs):
            _check_roots(cfg, why)
            return orig(cfg, *args, **kwargs)
        return _wrapped

    for target, why in ((harness_runs, "调用 runs.delete_provider"),
                        (harness_runs, "调用 runs.delete_model"),
                        (harness_runs, "调用 runs.purge_run"),
                        (harness_runs, "调用 runs.delete_run"),
                        (harness_sandbox, "调用 sandbox.destroy")):
        for name in ("delete_provider", "delete_model", "purge_run", "delete_run", "destroy"):
            original = getattr(target, name, None)
            if original is None:
                continue
            monkeypatch.setattr(target, name, _guard_callable(original, why), raising=False)
    yield


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

@pytest.fixture(scope="session")
def base_cfg():
    """以真实 config.json 为底，路径全部改指到临时工作区。"""
    cfg = harness_config.load()
    return dict(cfg)


@pytest.fixture
def workdir(tmp_path):
    """一个干净的临时工作区根目录。"""
    root = tmp_path / "work"
    root.mkdir()
    return str(root)


@pytest.fixture
def mini_repo(workdir):
    """把 fixtures/mini_repo 完整复制一份到临时目录（测试全程只动副本）。

    这里刻意不走 util.copy_tree：它按设计要跳过 node_modules，
    而夹具里的 node_modules/tiny-dep 作为依赖复制源需要保留。
    """
    dest = os.path.join(workdir, "repo")
    shutil.copytree(MINI_REPO, dest,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dest


@pytest.fixture
def packs_root(workdir):
    """把 fixtures/packs 复制一份到临时目录，作为本题用的 packs_root。"""
    dest = os.path.join(workdir, "packs")
    shutil.copytree(FIXTURE_PACKS, dest,
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    return dest



@pytest.fixture
def cfg(base_cfg, workdir, mini_repo, packs_root):
    """一份指向临时迷你仓库的配置；白名单补上要参与脱敏测试的两份文档。"""
    conf = dict(base_cfg)
    conf["repo_root"] = mini_repo
    # repos.* 是本机路径，config.json 里一写就会跟着进测试：健康检查会因为
    # 「别人机器上没有这个目录」而红，测试结论就不再可信。
    conf["repos"] = {}
    conf["packs_root"] = packs_root
    conf["sandbox_root"] = os.path.join(workdir, "sandboxes")
    conf["runs_root"] = os.path.join(workdir, "runs")
    conf["snapshot_cache"] = os.path.join(workdir, "sandboxes", ".snapshots")
    conf["static_root"] = os.path.join(workdir, "static")
    conf["snapshot"] = dict(base_cfg["snapshot"])
    conf["snapshot"]["docs_include"] = ["README.md", "AGENTS.md", "CHANGELOG.md"]
    conf["models"] = []
    conf["timeouts"] = dict(base_cfg["timeouts"])
    conf["timeouts"]["prepare_s"] = 120
    conf["timeouts"]["grade_default_s"] = 180
    conf["timeouts"]["grade_max_s"] = 900
    conf["max_concurrency"] = 3
    harness_config.ensure_workspace_dirs(conf)
    return conf


@pytest.fixture
def logs():
    """收集日志行，断言时能看清流程。"""
    return []


@pytest.fixture
def log(logs):
    def _log(message):
        logs.append(str(message))
    return _log


# --------------------------------------------------------------------------
# 常用动作
# --------------------------------------------------------------------------

def make_run(cfg, task_id=BACKEND_TASK, model="测试模型", attempt=1,
             run_id=None, run_dir=None):
    """造一个最小可用的 run 字典（不落盘，直接喂给 sandbox/grade）。"""
    run_dir = run_dir or os.path.join(cfg["runs_root"], "手工")
    return {
        "run_id": run_id or ("%s__%s__20260101-000000" % (task_id, util.sanitize_id(model))),
        "task": task_id,
        "model": model,
        "attempt": attempt,
        "status": "pending",
        "run_dir": run_dir,
        "created_at": "2026-01-01T00:00:00",
    }


def write_in_sandbox(sandbox, rel, text):
    """模拟模型在沙箱里改文件。"""
    path = os.path.join(sandbox, *rel.split("/"))
    util.ensure_dir(os.path.dirname(path))
    util.write_text_atomic(path, text)
    return path


#: 模型"改对了"的一份答案：写法与 reference/fix.patch 不同，用来验证部分分与相似度标记
MODEL_FULL_FIX = {
    "backend/miniapp/pricing.py": (
        '"""迷你派生指标：换手率与市值的唯一口径出处。\n'
        "\n"
        "同一份事实只在这里算一次，adapters 与 engine 都从这里取，\n"
        '避免"同一个事实被几处各自计算"（这道验收题要考的就是这个）。\n'
        '"""\n'
        "\n"
        'SPECIAL_SUFFIXES = ("ST", "退")\n'
        "\n"
        "\n"
        "def normalize_symbol(symbol):\n"
        '    """归一化股票代码：去掉市场后缀与空白。"""\n'
        '    return symbol.strip().split(".")[0].upper()\n'
        "\n"
        "\n"
        "def is_special_treated(symbol):\n"
        '    """是否 ST / *ST / 退市整理。"""\n'
        "    code = normalize_symbol(symbol)\n"
        "    return any(code.endswith(suffix) for suffix in SPECIAL_SUFFIXES)\n"
        "\n"
        "\n"
        "def turnover_rate(close, volume, shares):\n"
        '    """换手率 = 成交量 / 流通股本。"""\n'
        "    if shares <= 0:\n"
        "        return 0.0\n"
        "    # 分子是成交量，分母固定是流通股本，与收盘价无关\n"
        "    return round(volume / float(shares), 6)\n"
        "\n"
        "\n"
        "def market_cap(close, shares):\n"
        '    """总市值（亿元）= 收盘价 × 总股本。"""\n'
        "    return round(close * shares / 1e8, 6)\n"
    ),
    "backend/miniapp/adapters.py": (
        '"""行情行适配层：把一行行情变成派生列。\n'
        "\n"
        "派生列的口径必须与 pricing 一致：这里只做搬运与组装，不重新定义公式。\n"
        '"""\n'
        "\n"
        "from . import pricing\n"
        "\n"
        "\n"
        "def _turnover_of(row):\n"
        "    # 唯一的口径出处仍然是 pricing，适配层不自己写公式\n"
        "    return pricing.turnover_rate(\n"
        "        float(row['close']), float(row['volume']), float(row['shares'])\n"
        "    )\n"
        "\n"
        "\n"
        "def row_to_derived(row):\n"
        '    """一行行情 → 派生列（换手率、市值、是否 ST）。"""\n'
        "    code = pricing.normalize_symbol(row['symbol'])\n"
        "    close = float(row['close'])\n"
        "    shares = float(row['shares'])\n"
        "    return {\n"
        "        'symbol': code,\n"
        "        'close': close,\n"
        "        'turnover': _turnover_of(row),\n"
        "        'market_cap': pricing.market_cap(close, shares),\n"
        "        'special_treated': pricing.is_special_treated(code),\n"
        "    }\n"
        "\n"
        "\n"
        "def rows_to_derived(rows):\n"
        '    """批量转换。"""\n'
        "    return [row_to_derived(row) for row in rows]\n"
    ),
    "backend/miniapp/engine.py": (
        '"""选股引擎：按派生口径判定一只票能不能进候选池。\n'
        "\n"
        "阈值与口径都来自 pricing / adapters，本模块只做判定，不再重算派生值。\n"
        '"""\n'
        "\n"
        "from . import adapters, pricing\n"
        "\n"
        "TURNOVER_FLOOR = 0.01\n"
        "MARKET_CAP_FLOOR = 10.0\n"
        "\n"
        "\n"
        "def board_pass(row):\n"
        '    """进候选池的条件：非 ST，且换手率与市值同时过线。"""\n'
        "    derived = adapters.row_to_derived(row)\n"
        "    if derived['special_treated']:\n"
        "        return False\n"
        "    # 两个闸门缺一不可：只看市值会把低换手票放进来\n"
        "    turnover_ok = derived['turnover'] > TURNOVER_FLOOR\n"
        "    cap_ok = derived['market_cap'] > MARKET_CAP_FLOOR\n"
        "    return bool(turnover_ok and cap_ok)\n"
        "\n"
        "\n"
        "def screen_candidates(rows):\n"
        '    """筛出候选票（去重后按代码排序）。"""\n'
        "    picked = []\n"
        "    for row in rows:\n"
        "        if board_pass(row) and row['symbol'] not in picked:\n"
        "            picked.append(row['symbol'])\n"
        "    return sorted(picked)\n"
        "\n"
        "\n"
        "def summary_of(rows):\n"
        '    """候选池概览：给上层看板用。"""\n'
        "    return {\n"
        "        'total': len(rows),\n"
        "        'candidates': screen_candidates(rows),\n"
        "        'excluded_special': sum(\n"
        "            1 for r in rows if pricing.is_special_treated(r['symbol'])\n"
        "        ),\n"
        "    }\n"
    ),
}

#: 只修 pricing 一处（期望 33.3 分）：另两个端口还坏着
MODEL_PARTIAL_FIX = ("backend/miniapp/pricing.py", MODEL_FULL_FIX["backend/miniapp/pricing.py"])


@pytest.fixture
def apply_model_fix():
    """把上面那份"模型答案"写进沙箱。"""

    def _apply(sandbox, files=None):
        written = []
        for rel, text in (files if files is not None else MODEL_FULL_FIX).items():
            write_in_sandbox(sandbox, rel, text)
            written.append(rel)
        return written

    return _apply


@pytest.fixture
def snapshot_only(cfg, log):
    """只做快照，不碰沙箱。"""

    def _build(task_id=BACKEND_TASK):
        from harness import packs, snapshot
        meta = packs.load_meta(cfg, task_id)
        dest = os.path.join(cfg["snapshot_cache"], util.sanitize_id(task_id), "手工")
        return meta, dest, snapshot.build(cfg, meta, dest, log)

    return _build


def read_pack_meta(pack_root, task_id):
    """读题包 meta 的原始 JSON（测试里要核对写进去的字段）。"""
    path = os.path.join(pack_root, "core", "tasks", task_id, "meta.json")
    with open(path, "rb") as fh:
        return json.loads(fh.read().decode("utf-8"))
