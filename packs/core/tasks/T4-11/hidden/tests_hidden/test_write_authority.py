"""T4-11 隐藏测试（写入侧）：外部补数脚本 → 数据仓 → 服务健康 的整条链路。

七个出口：

* ``batching_exit``       —— 落盘次数与批大小相称，不是每票各写一次；
* ``amplification_exit``  —— 累计写盘量随总行数线性，不随分区已有行数放大；
* ``freshness_exit``      —— 写入必须打穿统计缓存，不靠 TTL 自然过期；
* ``lock_retry_exit``     —— 跨进程写锁的瞬时争用不得等于整批失败；
* ``health_exit``         —— 健康口径反映写入侧真实状况（含"扫描在跑"）；
* ``cross_process_exit``  —— 两个**真进程**写同一分区：行数守恒，不靠裸计时定序；
* ``coherence_write``     —— 脚本汇总 / 盘上真实 / 统计口径 / 健康口径对同一事实互恰。

隔离纪律：取数器换成进程内 stub，数据仓、进度文件与 HTTP 服务全部落在
``tmp_path``；跨进程用例用**文件栅栏**让"谁先落盘"确定，不用 sleep 赌时序。
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

# 回环流量绝不走系统代理（Windows 的注册表/环境变量代理会劫持本地请求）。
_OPENER = build_opener(ProxyHandler({}))

# 固定的 A 股交易日（2026-06-01 是周一），用例不依赖"今天"。
_WINDOW_START = "2026-06-01"
_WINDOW_END = "2026-06-05"


# ---------------------------------------------------------------------------
# 评分树定位 / 脚本加载 / 落盘探针
# ---------------------------------------------------------------------------


def _tree_root() -> Path:
    """向上找到评分树根（含 ``backend/astock_backtester`` 的那一层）。"""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "backend" / "astock_backtester" / "data" / "warehouse.py").is_file():
            return candidate
    raise RuntimeError("评分树根定位失败：向上找不到 backend/astock_backtester")


def _load_import_module(root: Path):
    """按路径加载补数脚本——文件名带连字符，不能直接 import。"""
    path = root / "scripts" / "run-full-market-import.py"
    spec = importlib.util.spec_from_file_location(f"t411_import_{uuid.uuid4().hex}", path)
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


class _StubFetcher:
    """本地取数器：每票 ``rows_per_symbol`` 行，零网络。"""

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


def _run_import(monkeypatch, module, cache_dir: Path, symbols, *, batch_size: int, rows_per_symbol: int = 1):
    """在进程内跑完补数脚本（取数器与股票名单都换成本地 stub）。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(module, "ADataProvider", _StubFetcher(rows_per_symbol=rows_per_symbol))
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
            _WINDOW_START,
            "--end-date",
            _WINDOW_END,
            "--source",
            "adata",
            "--workers",
            "1",
            "--write-batch-size",
            str(batch_size),
        ],
    )
    module.main()


def _partition_files(cache_dir: Path) -> list[Path]:
    return sorted((cache_dir / "warehouse" / "daily_bars").glob("year=*/daily_bars.parquet"))


def _disk_frame(cache_dir: Path) -> pd.DataFrame:
    paths = _partition_files(cache_dir)
    if not paths:
        return pd.DataFrame()
    frame = pd.concat([pd.read_parquet(path) for path in paths], ignore_index=True)
    frame["trade_date"] = pd.to_datetime(frame["trade_date"], errors="coerce").dt.normalize()
    return frame


def _disk_symbols(cache_dir: Path) -> set[str]:
    frame = _disk_frame(cache_dir)
    return set(frame["symbol"].astype(str)) if not frame.empty else set()


def _observe_partition_writes(monkeypatch) -> list[int]:
    """记录每一次"把一张表写成 parquet"的动作及其行数。

    观测点选在 ``pandas.DataFrame.to_parquet`` 这一层：无论实现走仓库的写入
    方法还是自己拼装，只要真的把行落到 parquet 就一定经过它，不绑定某个私有
    方法名（README 第 8 条：打桩式观测不得耦合实现的自由度）。
    """
    written: list[int] = []
    original = pd.DataFrame.to_parquet

    def counting(self, path=None, *args, **kwargs):
        written.append(int(len(self)))
        return original(self, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", counting)
    return written


def _finish_record(cache_dir: Path) -> dict:
    progress = cache_dir / "import-progress.jsonl"
    for line in reversed(progress.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("event") == "finish":
            return record
    raise AssertionError(f"进度文件里没有 finish 记录：{progress}")


# ===========================================================================
# 出口一：batching_exit —— 落盘次数与批大小相称
# ===========================================================================


def test_import_lands_one_partition_write_per_batch_not_per_symbol(tmp_path, monkeypatch):
    """25 只票 / 批大小 10：落盘次数应当远小于股票只数（按批落盘，不是每票一落）。"""
    module = _load_import_module(_tree_root())
    cache_dir = tmp_path / "cache"
    symbols = [f"6000{index:02d}" for index in range(1, 26)]
    written = _observe_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=10)

    assert len(written) <= 5, (
        f"25 只票按批大小 10 落盘 {len(written)} 次：攒批被拆成了每票一落"
    )
    assert _disk_symbols(cache_dir) == set(symbols), "按批落盘之后仍须覆盖全部股票"


def test_oversized_batch_lands_the_whole_pool_in_one_write(tmp_path, monkeypatch):
    """批大小远大于股票只数：整池只该有一次落盘（"批"语义的第二数据场景）。"""
    module = _load_import_module(_tree_root())
    cache_dir = tmp_path / "cache"
    symbols = [f"3000{index:02d}" for index in range(1, 8)]
    written = _observe_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=100)

    assert len(written) == 1, f"7 只票 / 批大小 100 落盘了 {len(written)} 次：{written}"
    assert len(_disk_frame(cache_dir)) == len(symbols)


# ===========================================================================
# 出口二：amplification_exit —— 累计写盘量随总行数线性
# ===========================================================================


def test_cumulative_written_rows_stay_linear_in_pool_size(tmp_path, monkeypatch):
    """12 只票 × 2 行、批大小 3：累计写盘量应在总行数量级，而不是平方级。"""
    module = _load_import_module(_tree_root())
    cache_dir = tmp_path / "cache"
    symbols = [f"0000{index:02d}" for index in range(1, 13)]
    written = _observe_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=3, rows_per_symbol=2)

    total_rows = len(symbols) * 2
    assert written, "落盘动作一次都没被观察到：数据进不了盘，或写入绕开了 parquet 写入层"
    assert sum(written) >= total_rows, f"累计写盘 {sum(written)} 行少于总行数 {total_rows}"
    assert sum(written) <= 3 * total_rows, (
        f"累计写盘 {sum(written)} 行远超总行数 {total_rows}：每次落盘都在重写整段分区"
    )
    assert len(_disk_frame(cache_dir)) == total_rows


def test_written_volume_does_not_grow_with_partition_row_count(tmp_path, monkeypatch):
    """每票 3 行、批大小 2：换一组行数，累计写盘量仍是总行数量级（第二数据场景）。"""
    module = _load_import_module(_tree_root())
    cache_dir = tmp_path / "cache"
    symbols = [f"0020{index:02d}" for index in range(1, 9)]
    written = _observe_partition_writes(monkeypatch)

    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=2, rows_per_symbol=3)

    total_rows = len(symbols) * 3
    assert written, "落盘动作一次都没被观察到：数据进不了盘，或写入绕开了 parquet 写入层"
    assert sum(written) >= total_rows, f"累计写盘 {sum(written)} 行少于总行数 {total_rows}"
    assert sum(written) <= 3 * total_rows, f"累计写盘 {sum(written)} 行远超总行数 {total_rows}"
    assert len(_disk_frame(cache_dir)) == total_rows


# ===========================================================================
# 出口三：freshness_exit —— 写入打穿统计缓存
# ===========================================================================


def test_write_makes_per_trade_date_counts_recompute(tmp_path):
    """写入之后"每交易日完整行数"必须重算，不得继续回放写入前的缓存。"""
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


def test_write_returns_the_symbol_pool_count_to_cold(tmp_path):
    """写入之后股票池计数缓存必须回到未热，现算一次拿到新计数。"""
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
# 出口四：lock_retry_exit —— 瞬时锁争用不得等于整批失败
# ===========================================================================


def test_one_transient_lock_timeout_is_absorbed(monkeypatch):
    """一次瞬时锁超时之后重试即成功，写入不得直接失败。"""
    from astock_backtester.data import operations
    from astock_backtester.data.filelock import FileLockTimeout

    monkeypatch.setattr(operations, "WRITE_LOCK_RETRY_BACKOFF_SECONDS", 0)
    attempts: list[int] = []

    def flaky_write():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise FileLockTimeout("daily_bars.parquet", 120.0)

    operations._write_with_lock_retry(flaky_write)

    assert attempts == [1, 2], f"一次瞬时锁超时应被吸收并重试，实际尝试序列 {attempts}"


def test_consecutive_lock_timeouts_are_absorbed(monkeypatch):
    """连续两次瞬时锁超时之后仍应写成功（第二数据场景）。"""
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


def test_import_route_survives_a_transient_lock_timeout(tmp_path, monkeypatch):
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
# 出口五：health_exit —— 健康口径反映写入侧真实状况
# ===========================================================================


def _get_json(port: int, path: str) -> dict:
    request = Request(
        f"http://127.0.0.1:{port}{path}",
        method="GET",
        headers={"Accept": "application/json"},
    )
    with _OPENER.open(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


class _LocalService:
    """一个回环上的本地数据服务（临时缓存目录），供健康口径用例复用。"""

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
        """取一次 /health；覆盖扫描还在跑就轮询到它落地。"""
        payload = _get_json(self.port, "/health")
        for _ in range(attempts):
            if not payload.get("coverage_refreshing"):
                return payload
            time.sleep(delay)
            payload = _get_json(self.port, "/health")
        return payload


def test_health_notices_rows_written_by_another_program(tmp_path):
    """另一个程序（不经 HTTP 路由）写库之后，健康口径必须跟上。"""
    service = _LocalService(tmp_path)
    try:
        service.health()  # 先把启动时的那一轮扫描走完

        service.server.state.warehouse.write_daily_bars(_bars(["600519", "000001"], ["2026-06-01"]))

        payload = service.health()
        datasets = {item["dataset"]: item for item in payload["coverage"]}
        assert "daily_bars" in datasets, f"健康快照缺少日线条目：{payload['coverage']}"
        assert datasets["daily_bars"]["symbols"] >= 2, (
            f"外部写入之后健康口径仍报 {datasets['daily_bars']['symbols']} 只：写入侧状况对外不可见"
        )
    finally:
        service.close()


def test_health_reports_a_write_side_scan_in_flight(tmp_path):
    """写入侧覆盖扫描正在跑时，健康口径必须如实报告"扫描在跑"。"""
    from astock_backtester.models import DatasetCoverage

    release = threading.Event()

    class SlowWarehouse:
        def coverage(self, **_kwargs):
            if not release.wait(timeout=60):
                raise AssertionError("coverage 扫描未被释放（用例收尾失败）")
            return [DatasetCoverage(dataset="daily_bars", symbols=9, start_date=None, end_date=None)]

    service = _LocalService(tmp_path)
    try:
        service.server.state.warehouse = SlowWarehouse()
        payload = _get_json(service.port, "/health")
        assert payload["coverage_refreshing"] is True, (
            "写入侧的覆盖扫描还没结束，健康口径却报告没有扫描在跑"
        )
    finally:
        release.set()
        service.close()


# ===========================================================================
# 出口六：cross_process_exit —— 两个真进程写同一分区，行数守恒
# ===========================================================================

_DRIVER_SOURCE = '''
"""T4-11 跨进程驱动：在真子进程里跑一次外部补数脚本（取数器为本地 stub）。

先后顺序由**文件栅栏**决定：某个进程可以在自己的第 N 次取数时写一个标记文件、
并在另一个标记出现前阻塞。于是"谁先落盘、谁后落盘"是确定的，不需要 sleep 赌时序。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pandas as pd


def _await(path: Path, timeout: float = 120.0) -> bool:
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
    fence_dir = Path(config["fence_dir"])
    fence_dir.mkdir(parents=True, exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "t411_driver", root / "scripts" / "run-full-market-import.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    counter = {"calls": 0}
    starts = list(config["start_dates"])

    class FencedFetcher:
        def fetch_daily_bars(self, symbol, start_date, end_date):
            counter["calls"] += 1
            call = counter["calls"]
            if config["raise_flag_at_call"] == call:
                (fence_dir / config["raise_flag"]).write_text("1", encoding="utf-8")
            if config["await_flag_at_call"] == call and config["await_flag"]:
                if not _await(fence_dir / config["await_flag"]):
                    raise SystemExit("等待栅栏超时：" + config["await_flag"])
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

    module.ADataProvider = FencedFetcher
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
    (fence_dir / config["done_flag"]).write_text("1", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _spawn(script: Path, config: dict):
    return subprocess.Popen(
        [sys.executable, str(script), json.dumps(config, ensure_ascii=False)],
        cwd=str(script.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _join(process, label: str, timeout: float = 240.0) -> str:
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        raise AssertionError(f"{label}未在 {timeout:.0f}s 内结束：\n{output[-2000:]}") from None
    assert process.returncode == 0, f"{label}退出码 {process.returncode}：\n{output[-2000:]}"
    return output


def _driver_config(root: Path, cache_dir: Path, fence_dir: Path, **overrides) -> dict:
    config = {
        "root": str(root),
        "cache_dir": str(cache_dir),
        "fence_dir": str(fence_dir),
        "start_date": _WINDOW_START,
        "end_date": _WINDOW_END,
        "rows_per_symbol": 1,
        "batch_size": 1,
        "start_dates": [_WINDOW_START],
        "symbols": [],
        "raise_flag_at_call": 0,
        "raise_flag": "unused",
        "await_flag_at_call": 0,
        "await_flag": "",
        "done_flag": "done",
    }
    config.update(overrides)
    return config


def _fenced_setup(tmp_path: Path):
    script = tmp_path / "driver.py"
    script.write_text(_DRIVER_SOURCE, encoding="utf-8")
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    fence_dir = tmp_path / "fences"
    fence_dir.mkdir()
    return _tree_root(), script, cache_dir, fence_dir


def test_two_programs_writing_one_partition_conserve_every_row(tmp_path):
    """两个进程各自写自己那批股票：先落盘的进程写下的行不得被后落盘的抹掉。"""
    root, script, cache_dir, fence_dir = _fenced_setup(tmp_path)

    early_symbols = ["601001", "601002", "601003", "601004"]
    late_symbols = ["603001", "603002", "603003"]

    # early 取到第一只票时打招呼；late 取到第一只票后等 early 全部写完再继续。
    early = _spawn(
        script,
        _driver_config(
            root,
            cache_dir,
            fence_dir,
            symbols=early_symbols,
            raise_flag_at_call=1,
            raise_flag="early-started",
            done_flag="early-done",
        ),
    )
    late = _spawn(
        script,
        _driver_config(
            root,
            cache_dir,
            fence_dir,
            symbols=late_symbols,
            await_flag_at_call=1,
            await_flag="early-done",
            done_flag="late-done",
        ),
    )

    _join(late, "后落盘的进程")
    _join(early, "先落盘的进程")

    on_disk = _disk_symbols(cache_dir)
    expected = set(early_symbols) | set(late_symbols)
    assert on_disk == expected, (
        f"两个进程写同一分区之后盘上只有 {sorted(on_disk)}，丢了 {sorted(expected - on_disk)}"
    )
    assert len(_disk_frame(cache_dir)) == len(expected)


def test_two_programs_appending_different_dates_conserve_every_row(tmp_path):
    """两个进程给同一只票补不同交易日：两个进程的行都要在盘上（第二数据场景）。"""
    root, script, cache_dir, fence_dir = _fenced_setup(tmp_path)
    symbol = "600519"

    early = _spawn(
        script,
        _driver_config(
            root,
            cache_dir,
            fence_dir,
            symbols=[symbol],
            start_dates=["2026-06-01"],
            done_flag="early-done",
        ),
    )
    late = _spawn(
        script,
        _driver_config(
            root,
            cache_dir,
            fence_dir,
            symbols=[symbol],
            start_dates=["2026-06-03"],
            await_flag_at_call=1,
            await_flag="early-done",
            done_flag="late-done",
        ),
    )

    _join(late, "后落盘的进程")
    _join(early, "先落盘的进程")

    frame = _disk_frame(cache_dir)
    assert set(frame["symbol"].astype(str)) == {symbol}, f"盘上股票不对：{frame}"
    dates = sorted(frame["trade_date"].dt.strftime("%Y-%m-%d"))
    assert dates == ["2026-06-01", "2026-06-03"], (
        f"同一只票的两个交易日没有都留下：{dates}（后落盘的进程抹掉了先落盘的行）"
    )


# ===========================================================================
# 出口七：coherence_write —— 多出口对同一事实口径一致
# ===========================================================================


def test_script_summary_matches_disk_and_statistics(tmp_path, monkeypatch):
    """脚本汇总 / 盘上真实 / 统计口径三处对"这次补了多少行"必须同一个数。"""
    from astock_backtester.data.warehouse import Warehouse

    module = _load_import_module(_tree_root())
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 补数脚本与随后的读取共用同一个仓库句柄——桌面端"写完顺手看一眼统计"的形态。
    warehouse = Warehouse(cache_dir)
    monkeypatch.setattr(module, "Warehouse", lambda cache_root: warehouse)
    assert sum(warehouse.market_trade_date_counts(_WINDOW_START, _WINDOW_END).values()) == 0

    symbols = [f"6001{index:02d}" for index in range(1, 7)]
    _run_import(monkeypatch, module, cache_dir, symbols, batch_size=2, rows_per_symbol=2)

    disk_rows = len(_disk_frame(cache_dir))
    record = _finish_record(cache_dir)

    assert record["imported_rows"] == disk_rows, (
        f"脚本汇总 {record['imported_rows']} 行 ≠ 盘上真实 {disk_rows} 行"
    )
    counts_total = sum(warehouse.market_trade_date_counts(_WINDOW_START, _WINDOW_END).values())
    assert counts_total == disk_rows, (
        f"统计口径 {counts_total} 行 ≠ 盘上真实 {disk_rows} 行：写入没有让统计口径失效"
    )


def test_health_symbol_count_matches_what_the_script_wrote(tmp_path, monkeypatch):
    """外部脚本写完之后，健康口径报的股票只数必须等于盘上真实只数。"""
    module = _load_import_module(_tree_root())
    service = _LocalService(tmp_path)
    try:
        service.health()  # 空仓时的初始扫描

        symbols = [f"0000{index:02d}" for index in range(1, 5)]
        _run_import(monkeypatch, module, tmp_path, symbols, batch_size=2)

        payload = service.health()
        datasets = {item["dataset"]: item for item in payload["coverage"]}
        on_disk = _disk_symbols(tmp_path)
        assert on_disk == set(symbols), f"脚本没有把全部股票写进分区：{sorted(on_disk)}"
        assert "daily_bars" in datasets, f"健康快照缺少日线条目：{payload['coverage']}"
        assert datasets["daily_bars"]["symbols"] == len(on_disk), (
            f"健康口径 {datasets['daily_bars']['symbols']} 只 ≠ 盘上真实 {len(on_disk)} 只"
        )
    finally:
        service.close()
