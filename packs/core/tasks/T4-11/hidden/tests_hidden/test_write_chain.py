"""T3-10 隐藏测试：外部补数脚本 → 数据仓 → 服务健康 的整条写入链路。

六个出口（组）：

* ``batching_exit``       —— 落盘次数与批大小相称，不是每票各写一次；
* ``amplification_exit``  —— 累计写盘量随批数线性，不随"批数 × 分区行数"平方放大；
* ``cross_process_exit``  —— 两个**真进程**写同一分区：零丢失（行数守恒，禁裸计时）；
* ``freshness_exit``      —— 写入后统计口径立刻反映新数据，不靠 TTL 兜底；
* ``health_exit``         —— 健康口径反映写入侧真实状况（含"正在刷新"）；
* ``lock_retry_exit``     —— 跨进程写锁的瞬时争用不得等于整批失败；
* ``coherence``           —— 脚本汇总 / 落盘行数 / 统计口径 / 健康口径对同一事实一致。

网络隔离：脚本的取数器被替换成本地 stub，数据仓、进度文件与 HTTP 服务全部落在
``tmp_path`` 下，任何用例都不发起出站请求。并发用例用**文件栅栏**（子进程互相
等一个标记文件）制造确定的交错顺序，不用 sleep 竞速。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd

# 回环流量绝不走系统代理（Windows 注册表/环境变量代理会劫持本地请求）。
_OPENER = build_opener(ProxyHandler({}))

# 一批固定的 A 股交易日（2026-06-01 是周一），避免用例依赖"今天"。
_START = "2026-06-01"
_END = "2026-06-05"


# ---------------------------------------------------------------------------
# 评分树定位与脚本加载
# ---------------------------------------------------------------------------


def _tree_root() -> Path:
    """向上找到评分树根（含 ``backend/astock_backtester`` 的那一层）。

    隐藏测试在真 harness 里位于 ``<树>/hidden/tests_hidden/``，在出题侧自验里
    位于 ``<树>/tests_hidden/``；两种布局下都向上找得到同一棵树。
    """
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "astock_backtester" / "data" / "warehouse.py").is_file():
            return candidate
    raise RuntimeError("评分树根定位失败：向上找不到 backend/astock_backtester")


def _load_import_module(root: Path):
    """按路径加载补数脚本——文件名带连字符，不能直接 import。"""
    path = root / "scripts" / "run-full-market-import.py"
    spec = importlib.util.spec_from_file_location(f"t310_import_{uuid.uuid4().hex}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bars(symbols, dates, base: float = 10.0) -> pd.DataFrame:
    rows = []
    for symbol in symbols:
        for day in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": day,
                    "open": base,
                    "high": base + 0.5,
                    "low": base - 0.5,
                    "close": base + 0.2,
                    "volume": 1000,
                }
            )
    return pd.DataFrame(rows)


class _StubProvider:
    """本地取数器：按 symbol 返回 ``rows_per_symbol`` 行，零网络。

    脚本里 ``ADataProvider()`` 是"无参构造 + 实例方法"，所以这个 stub 自己也
    可调用（``__call__`` 返回自身），构造与取数都落在同一个计数器上。
    """

    def __init__(self, rows_per_symbol: int = 1, hook=None) -> None:
        self.rows_per_symbol = rows_per_symbol
        self.hook = hook
        self.calls = 0

    def __call__(self):
        return self

    def fetch_daily_bars(self, symbol, start_date, end_date):
        self.calls += 1
        if self.hook is not None:
            self.hook(self.calls, symbol)
        dates = pd.bdate_range(start_date, periods=self.rows_per_symbol)
        count = len(dates)
        return pd.DataFrame(
            {
                "symbol": [symbol] * count,
                "trade_date": dates.strftime("%Y-%m-%d"),
                "open": [10.0] * count,
                "high": [10.5] * count,
                "low": [9.8] * count,
                "close": [10.2] * count,
                "volume": [1000] * count,
            }
        )


def _run_import(
    monkeypatch,
    module,
    cache_dir: Path,
    symbols,
    *,
    batch_size: int,
    rows_per_symbol: int = 1,
    provider=None,
):
    """在进程内把补数脚本跑完（取数器与股票名单都换成 stub，零网络）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    provider = provider if provider is not None else _StubProvider(rows_per_symbol=rows_per_symbol)
    monkeypatch.setattr(module, "ADataProvider", provider)
    monkeypatch.setattr(
        module,
        "load_symbols",
        lambda cache_root, provider, refresh=False: (list(symbols), "hidden-test"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run-full-market-import.py",
            "--cache-dir",
            str(cache_dir),
            "--start-date",
            _START,
            "--end-date",
            _END,
            "--source",
            "adata",
            "--workers",
            "1",
            "--write-batch-size",
            str(batch_size),
        ],
    )
    module.main()
    return provider


# ---------------------------------------------------------------------------
# 落盘探针（读侧只认磁盘，不看实现）
# ---------------------------------------------------------------------------


def _partition_paths(cache_dir: Path) -> list[Path]:
    return sorted((cache_dir / "warehouse" / "daily_bars").glob("year=*/daily_bars.parquet"))


def _disk_frame(cache_dir: Path) -> pd.DataFrame:
    paths = _partition_paths(cache_dir)
    if not paths:
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    return frame


def _disk_symbols(cache_dir: Path) -> set[str]:
    frame = _disk_frame(cache_dir)
    return set(frame["symbol"].astype(str)) if not frame.empty else set()


def _count_partition_writes(monkeypatch) -> list[int]:
    """记录每次分区落盘的行数——写盘量是这条链路上唯一可信的放大证据。"""
    from astock_backtester.data.warehouse import Warehouse

    written: list[int] = []
    original = Warehouse._atomic_write_parquet

    def counting(frame, path):
        written.append(len(frame))
        return original(frame, path)

    monkeypatch.setattr(Warehouse, "_atomic_write_parquet", staticmethod(counting))
    return written


def _finish_event(progress_path: Path) -> dict:
    for line in reversed(progress_path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("event") == "finish":
            return event
    raise AssertionError(f"进度文件里没有 finish 事件：{progress_path}")


# ===========================================================================
# 1. batching_exit —— 落盘次数与批大小相称
# ===========================================================================


def test_script_writes_once_per_batch_not_once_per_symbol(tmp_path, monkeypatch):
    """25 只票 / 批大小 10 → 3 次落盘；落盘次数等于股票只数说明攒批被拆掉了。"""
    root = _tree_root()
    module = _load_import_module(root)
    cache_dir = tmp_path / "cache"
    symbols = [f"6000{index:02d}" for index in range(1, 26)]
    written = _count_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=10)

    assert len(written) <= 5, f"25 只票按批大小 10 应只落盘 3 次，实际 {len(written)} 次：{written}"
    assert _disk_symbols(cache_dir) == set(symbols), "攒批之后仍然要覆盖全部股票"


def test_batch_larger_than_the_symbol_count_lands_in_one_write(tmp_path, monkeypatch):
    """批大小远大于股票只数 → 一次落盘；这是"批"这个语义的第二数据场景。"""
    root = _tree_root()
    module = _load_import_module(root)
    cache_dir = tmp_path / "cache"
    symbols = [f"3000{index:02d}" for index in range(1, 8)]
    written = _count_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=100)

    assert len(written) == 1, f"7 只票 / 批大小 100 应一次落盘，实际 {len(written)} 次：{written}"
    assert len(_disk_frame(cache_dir)) == len(symbols)


# ===========================================================================
# 2. amplification_exit —— 写盘量随批数线性
# ===========================================================================


def test_rows_written_stay_linear_in_total_rows(tmp_path, monkeypatch):
    """12 只票 × 2 行、批大小 3 → 累计写盘量应是总行数量级，而不是平方级。"""
    root = _tree_root()
    module = _load_import_module(root)
    cache_dir = tmp_path / "cache"
    symbols = [f"0000{index:02d}" for index in range(1, 13)]
    written = _count_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=3, rows_per_symbol=2)

    total_rows = len(symbols) * 2
    assert sum(written) <= 3 * total_rows, (
        f"累计写盘 {sum(written)} 行远超总行数 {total_rows}（每次落盘都在重写整段分区）"
    )
    assert len(_disk_frame(cache_dir)) == total_rows


def test_amplification_does_not_depend_on_partition_size(tmp_path, monkeypatch):
    """换一组行数（每票 3 行、批大小 2）：写盘量仍应是总行数量级。"""
    root = _tree_root()
    module = _load_import_module(root)
    cache_dir = tmp_path / "cache"
    symbols = [f"0020{index:02d}" for index in range(1, 9)]
    written = _count_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=2, rows_per_symbol=3)

    total_rows = len(symbols) * 3
    assert sum(written) <= 3 * total_rows, (
        f"累计写盘 {sum(written)} 行远超总行数 {total_rows}"
    )
    assert len(_disk_frame(cache_dir)) == total_rows


# ===========================================================================
# 3. freshness_exit —— 写入后统计口径立刻失效
# ===========================================================================


def test_write_invalidates_trade_date_counts(tmp_path):
    """写入之后"每交易日行数"必须重算，而不是继续回放写入前的缓存。"""
    from astock_backtester.data.warehouse import Warehouse

    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars(["000001"], ["2026-06-01"]))
    before = warehouse.market_trade_date_counts("2026-06-01", "2026-06-03")
    assert before["2026-06-02"] == 0

    warehouse.write_daily_bars(_bars(["000002"], ["2026-06-02"]))

    after = warehouse.market_trade_date_counts("2026-06-01", "2026-06-03")
    assert after["2026-06-02"] == 1, (
        f"写入之后统计口径仍停在旧值：{after}（写入没有让统计缓存失效）"
    )


def test_write_invalidates_symbol_pool_count(tmp_path):
    """写入之后股票池计数缓存必须回到未热，再算一次拿到新计数。"""
    from astock_backtester.data.warehouse import Warehouse

    warehouse = Warehouse(tmp_path)
    assert warehouse.refresh_symbol_count() == 0
    assert warehouse.cached_symbol_count() == 0

    warehouse.write_daily_bars(_bars(["600000", "000001"], ["2026-06-01"]))

    assert warehouse.cached_symbol_count() is None, (
        f"写入之后股票池计数缓存没有失效：{warehouse.cached_symbol_count()}"
    )
    assert warehouse.refresh_symbol_count() == 2


# ===========================================================================
# 4. lock_retry_exit —— 瞬时写锁争用不得等于整批失败
# ===========================================================================


def test_single_transient_lock_timeout_is_retried(monkeypatch):
    """一次瞬时锁超时后重试即成功，写入不得直接失败。"""
    from astock_backtester.data import operations
    from astock_backtester.data.filelock import FileLockTimeout

    monkeypatch.setattr(operations, "WRITE_LOCK_RETRY_BACKOFF_SECONDS", 0)
    attempts: list[int] = []

    def flaky_write():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise FileLockTimeout("daily_bars.parquet", 120.0)

    operations._write_with_lock_retry(flaky_write)

    assert attempts == [1, 2], f"一次瞬时锁超时应被重试，实际尝试序列 {attempts}"


def test_repeated_lock_contention_still_settles(monkeypatch):
    """连续两次瞬时锁超时后仍应写成功——这是第二条数据场景。"""
    from astock_backtester.data import operations
    from astock_backtester.data.filelock import FileLockTimeout

    monkeypatch.setattr(operations, "WRITE_LOCK_RETRY_BACKOFF_SECONDS", 0)
    attempts: list[int] = []

    def flaky_write():
        attempts.append(len(attempts) + 1)
        if len(attempts) <= 2:
            raise FileLockTimeout("daily_bars.parquet", 120.0)

    operations._write_with_lock_retry(flaky_write)

    assert attempts == [1, 2, 3], f"两次瞬时锁超时后应写成功，实际尝试序列 {attempts}"


def test_daily_bars_import_survives_a_transient_lock_timeout(tmp_path, monkeypatch):
    """端到端：一次瞬时锁超时不得让整批日线被丢掉。"""
    from astock_backtester.data import operations
    from astock_backtester.data.cache import LocalCache
    from astock_backtester.data.filelock import FileLockTimeout

    monkeypatch.setattr(operations, "WRITE_LOCK_RETRY_BACKOFF_SECONDS", 0)
    cache = LocalCache(tmp_path)
    original_write = LocalCache.write_daily_bars
    attempts: list[int] = []

    def flaky_write(self, frame):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise FileLockTimeout(str(self.daily_bars_path), 120.0)
        return original_write(self, frame)

    monkeypatch.setattr(LocalCache, "write_daily_bars", flaky_write)

    result = operations.import_daily_bars_into_cache(
        cache=cache,
        frame=_bars(["AAA"], ["2026-06-01"]),
        source="hidden-test",
    )

    assert result.status == "ok", f"瞬时锁超时把整批判成了失败：{result.status}"
    assert len(cache.read_daily_bars()) == 1, "重试成功之后数据必须真的在缓存里"


# ===========================================================================
# 5. health_exit —— 健康口径反映写入侧真实状况
# ===========================================================================


def _get_json(port: int, path: str) -> dict:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    with _OPENER.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class _ServerHarness:
    """起一个本地数据服务（回环、临时缓存目录），供健康口径用例复用。"""

    def __init__(self, cache_dir: Path) -> None:
        from astock_backtester.service import create_server

        self.server = create_server(host="127.0.0.1", port=0, cache_dir=cache_dir)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=20)

    def health(self, attempts: int = 60, delay: float = 0.1) -> dict:
        """取一次 /health；若覆盖刷新还在跑，继续轮询到它落地。"""
        payload = _get_json(self.port, "/health")
        for _ in range(attempts):
            if not payload.get("coverage_refreshing"):
                return payload
            time.sleep(delay)
            payload = _get_json(self.port, "/health")
        return payload


def test_health_sees_rows_written_by_another_program(tmp_path):
    """另一个程序（不经 HTTP 路由）写库之后，健康口径必须跟上。"""
    harness = _ServerHarness(tmp_path)
    try:
        harness.health()  # 先把启动时的那一轮刷新走完

        harness.server.state.warehouse.write_daily_bars(_bars(["600519", "000001"], ["2026-06-01"]))

        payload = harness.health()
        datasets = {item["dataset"]: item for item in payload["coverage"]}
        assert "daily_bars" in datasets, f"健康快照缺少日线条目：{payload['coverage']}"
        assert datasets["daily_bars"]["symbols"] >= 2, (
            "外部写入之后健康口径仍报 0 只股票：写入侧状况对外不可见"
        )
    finally:
        harness.close()


def test_health_reports_an_in_flight_write_side_scan(tmp_path):
    """写入侧覆盖扫描正在跑时，健康口径必须如实报告"刷新在跑"。"""
    from astock_backtester.models import DatasetCoverage

    release = threading.Event()

    class SlowWarehouse:
        def coverage(self, **_kwargs):
            if not release.wait(timeout=60):
                raise AssertionError("coverage 扫描未被释放（用例收尾失败）")
            return [
                DatasetCoverage(dataset="daily_bars", symbols=9, start_date=None, end_date=None),
            ]

    harness = _ServerHarness(tmp_path)
    try:
        harness.server.state.warehouse = SlowWarehouse()
        payload = _get_json(harness.port, "/health")
        assert payload["coverage_refreshing"] is True, (
            "写入侧的覆盖扫描还没结束，健康口径却报告没有刷新在跑"
        )
    finally:
        release.set()
        harness.close()


# ===========================================================================
# 6. coherence —— 多出口对同一事实口径一致
# ===========================================================================


def test_script_summary_disk_and_counts_agree(tmp_path, monkeypatch):
    """脚本汇总 / 实际落盘 / 写入方自己的统计口径三处对"这次补了多少行"必须同一个数。"""
    from astock_backtester.data.warehouse import Warehouse

    root = _tree_root()
    module = _load_import_module(root)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 补数脚本与随后的读取共用同一个仓库句柄——桌面端"写完顺手看一眼统计"的形态。
    warehouse = Warehouse(cache_dir)
    monkeypatch.setattr(module, "Warehouse", lambda cache_root: warehouse)
    assert sum(warehouse.market_trade_date_counts(_START, _END).values()) == 0

    symbols = [f"6001{index:02d}" for index in range(1, 7)]
    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=2, rows_per_symbol=2)

    disk_rows = len(_disk_frame(cache_dir))
    summary = _finish_event(cache_dir / "import-progress.jsonl")

    assert summary["imported_rows"] == disk_rows, (
        f"脚本汇总 {summary['imported_rows']} 行 ≠ 实际落盘 {disk_rows} 行"
    )
    counts_total = sum(warehouse.market_trade_date_counts(_START, _END).values())
    assert counts_total == disk_rows, (
        f"统计口径 {counts_total} 行 ≠ 落盘 {disk_rows} 行：写入没有让统计口径失效"
    )


def test_health_coverage_agrees_with_script_written_rows(tmp_path, monkeypatch):
    """外部脚本写完之后，健康口径报的股票只数必须等于盘上真实只数。"""
    root = _tree_root()
    module = _load_import_module(root)
    harness = _ServerHarness(tmp_path)
    try:
        harness.health()  # 空仓时的初始刷新

        symbols = [f"0000{index:02d}" for index in range(1, 5)]
        _run_import(monkeypatch, module, tmp_path, symbols, batch_size=2)

        payload = harness.health()
        datasets = {item["dataset"]: item for item in payload["coverage"]}
        on_disk = _disk_symbols(tmp_path)
        assert on_disk == set(symbols), f"脚本没有把全部股票写进分区：{sorted(on_disk)}"
        assert "daily_bars" in datasets, f"健康快照缺少日线条目：{payload['coverage']}"
        assert datasets["daily_bars"]["symbols"] == len(on_disk), (
            f"健康口径 {datasets['daily_bars']['symbols']} 只 ≠ 盘上真实 {len(on_disk)} 只"
        )
    finally:
        harness.close()


# ===========================================================================
# 7. cross_process_exit —— 两个真进程写同一分区：零丢失
# ===========================================================================

_DRIVER_SOURCE = '''
"""T3-10 跨进程驱动：在真子进程里跑一次外部补数脚本（取数器为本地 stub）。

进程间的先后顺序由**文件栅栏**决定：每个进程在自己的第 N 次取数时写一个标记
文件、并在另一个标记出现前阻塞。这样"谁先读分区、谁后写分区"是确定的，不需要
靠 sleep 去赌时序。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pandas as pd


def _wait(path: Path, timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def main() -> int:
    config = json.loads(sys.argv[1])
    root = Path(config["root"])
    cache_dir = Path(config["cache_dir"])
    gate_dir = Path(config["gate_dir"])
    gate_dir.mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "t310_import_driver", root / "scripts" / "run-full-market-import.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    state = {"calls": 0}
    starts = list(config["start_dates"])

    class StubProvider:
        def fetch_daily_bars(self, symbol, start_date, end_date):
            state["calls"] += 1
            call = state["calls"]
            if config["signal_after_calls"] == call:
                (gate_dir / config["signal_name"]).write_text("1", encoding="utf-8")
            if config["wait_after_calls"] == call and config["wait_name"]:
                if not _wait(gate_dir / config["wait_name"]):
                    raise SystemExit("等待栅栏超时：" + config["wait_name"])
            start = starts[min(call - 1, len(starts) - 1)]
            dates = pd.bdate_range(start, periods=config["rows_per_symbol"])
            count = len(dates)
            return pd.DataFrame(
                {
                    "symbol": [symbol] * count,
                    "trade_date": dates.strftime("%Y-%m-%d"),
                    "open": [10.0] * count,
                    "high": [10.5] * count,
                    "low": [9.8] * count,
                    "close": [10.2] * count,
                    "volume": [1000] * count,
                }
            )

    module.ADataProvider = StubProvider
    module.load_symbols = lambda cache_root, provider, refresh=False: (list(config["symbols"]), "driver")
    sys.argv = [
        "run-full-market-import.py",
        "--cache-dir", str(cache_dir),
        "--start-date", config["start_date"],
        "--end-date", config["end_date"],
        "--source", "adata",
        "--workers", "1",
        "--write-batch-size", str(config["batch_size"]),
    ]
    module.main()
    (gate_dir / config["done_name"]).write_text("1", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _spawn_driver(driver: Path, config: dict):
    return subprocess.Popen(
        [sys.executable, str(driver), json.dumps(config, ensure_ascii=False)],
        cwd=str(driver.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _finish_driver(process, label: str, timeout: float = 180.0) -> str:
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        raise AssertionError(f"{label}未在 {timeout:.0f}s 内结束：\n{output[-2000:]}") from None
    assert process.returncode == 0, f"{label}退出码 {process.returncode}：\n{output[-2000:]}"
    return output


def _driver_config(root: Path, cache_dir: Path, gate_dir: Path, **overrides) -> dict:
    config = {
        "root": str(root),
        "cache_dir": str(cache_dir),
        "gate_dir": str(gate_dir),
        "start_date": _START,
        "end_date": _END,
        "rows_per_symbol": 1,
        "batch_size": 1,
        "start_dates": [_START],
        "symbols": [],
        "signal_after_calls": 0,
        "signal_name": "unused",
        "wait_after_calls": 0,
        "wait_name": "",
        "done_name": "done",
    }
    config.update(overrides)
    return config


def test_two_processes_writing_one_partition_keep_every_row(tmp_path):
    """第二个写入方先落盘、再冻结一段时间：期间第一个写入方的行不得被抹掉。"""
    root = _tree_root()
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()

    first_symbols = ["601001", "601002", "601003", "601004"]
    second_symbols = ["603001", "603002", "603003"]

    # 顺序：second 落第一批 → first 全程跑完 → second 才继续写。
    second = _spawn_driver(
        driver,
        _driver_config(
            root,
            cache_dir,
            gate_dir,
            symbols=second_symbols,
            batch_size=1,
            signal_after_calls=2,
            signal_name="second-flushed-once",
            wait_after_calls=2,
            wait_name="first-done",
            done_name="second-done",
        ),
    )
    first = _spawn_driver(
        driver,
        _driver_config(
            root,
            cache_dir,
            gate_dir,
            symbols=first_symbols,
            batch_size=2,
            signal_after_calls=1,
            signal_name="first-started",
            wait_after_calls=1,
            wait_name="second-flushed-once",
            done_name="first-done",
        ),
    )

    _finish_driver(first, "第一个写入方")
    _finish_driver(second, "第二个写入方")

    present = _disk_symbols(cache_dir)
    expected = set(first_symbols) | set(second_symbols)
    missing = sorted(expected - present)
    assert not missing, f"两个进程写同一分区丢了 {len(missing)} 只：{missing}"
    assert len(_disk_frame(cache_dir)) == len(expected), "落盘行数与两只进程报的写入量不符"


def test_two_processes_appending_to_one_symbol_keep_every_date(tmp_path):
    """第二数据场景：两个进程补**同一只股票**的不同交易日，日期不得互相覆盖。"""
    root = _tree_root()
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER_SOURCE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    gate_dir = tmp_path / "gates"
    gate_dir.mkdir()

    second = _spawn_driver(
        driver,
        _driver_config(
            root,
            cache_dir,
            gate_dir,
            symbols=["600519", "600519"],
            batch_size=1,
            start_dates=["2026-06-03", "2026-06-04"],
            signal_after_calls=2,
            signal_name="second-flushed-once",
            wait_after_calls=2,
            wait_name="first-done",
            done_name="second-done",
        ),
    )
    first = _spawn_driver(
        driver,
        _driver_config(
            root,
            cache_dir,
            gate_dir,
            symbols=["600519", "600519"],
            batch_size=1,
            start_dates=["2026-06-01", "2026-06-02"],
            wait_after_calls=1,
            wait_name="second-flushed-once",
            done_name="first-done",
        ),
    )

    _finish_driver(first, "第一个写入方")
    _finish_driver(second, "第二个写入方")

    frame = _disk_frame(cache_dir)
    dates = sorted(frame["trade_date"].dt.strftime("%Y-%m-%d").tolist())
    assert dates == ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"], (
        f"同一只股票的两个补数进程互相覆盖了日期，盘上只有 {dates}"
    )
