"""T2-06 隐藏测试：并发写仓与损坏暴露。

只断言外部可观察的行为：互斥在两个真进程之间是否成立、落盘中途读者看到
什么、坏分区在读路径与诊断口径上的呈现。不约束实现住在哪个模块、用什么
原语。

进程级用例的确定性手法沿用仓库既有测试的双进程模式：子进程 stdout 做
消息握手（父进程等到"HELD"才开始动作），不依赖 sleep 时序，不碰网络。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd
import pyarrow.parquet as pq
import pytest

from astock_backtester.data.filelock import CrossProcessFileLock, FileLockTimeout
from astock_backtester.data.warehouse import Warehouse

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = _REPO_ROOT / "backend"

# 回环流量绝不走系统代理
_OPENER = build_opener(ProxyHandler({}))


def _get_json(port: int, path: str, *, allow_error: bool = False) -> dict:
    request = Request(f"http://127.0.0.1:{port}{path}", method="GET",
                      headers={"Accept": "application/json"})
    try:
        with _OPENER.open(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if not allow_error:
            raise
        return json.loads(exc.read().decode("utf-8"))


def _bars(symbol: str, start: str, count: int) -> pd.DataFrame:
    dates = pd.bdate_range(start, periods=count)
    return pd.DataFrame({
        "symbol": [symbol] * count,
        "trade_date": dates.strftime("%Y-%m-%d"),
        "open": [10.0] * count,
        "high": [10.5] * count,
        "low": [9.8] * count,
        "close": [10.2] * count,
        "volume": [1000] * count,
    })


def _partition(root: Path, year: int) -> Path:
    return root / "warehouse" / "daily_bars" / f"year={year}" / "daily_bars.parquet"


def _corrupt_partition(root: Path, year: int) -> Path:
    """放一个人工坏分区（不是 parquet 的字节流），返回分区路径。"""
    path = _partition(root, year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a parquet file")
    return path


def _spawn(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, str(_BACKEND), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _written_path(path) -> Path | None:
    """把 ``DataFrame.to_parquet`` 的 ``path`` 实参解析成可比对的路径。

    pandas 的 ``to_parquet`` 明确允许 ``path`` 是"str、路径对象**或文件对象**"
    （文件对象形态用于自己控制落盘/flush/fsync，是完全正当的写法）。文件对象
    上做的 ``Path(str(path))`` 只会得到 ``<_io.BufferedWriter name=...>`` 这种
    字符串，与目标路径永不相等——于是打桩把一次**暂存写**误判成直写目标，
    再对同一个已打开的流做两次写入（半截 + 完整），两个 parquet 首尾相接，
    读出来报 ``Column cannot have more than one dictionary``。这是 2026-10-02
    那次假阴性（要求临时文件名前缀匹配）的同族残留：判据问的是"参数长什么样"，
    不是"这次落盘是不是作用在目标文件本身"。

    解析不出路径（文件对象、整数 fd）时返回 ``None``，由调用方按
    "不是目标本身"处理 —— 也就是按暂存写处理，与真实行为一致。
    """
    raw = path if isinstance(path, (str, os.PathLike)) else getattr(path, "name", None)
    if isinstance(raw, int) or not isinstance(raw, (str, bytes)):
        return None
    return Path(os.fsdecode(raw))


def _reset_destination(path) -> None:
    """把落盘目标复位，使"两次 ``to_parquet`` 调用"在两种入参下等价。

    ``to_parquet`` 拿到**路径**时，每次调用都以 ``'wb'`` 重新打开（先截断），
    所以"先写一半、再写完整"这两次调用之间目标是被清空的——这正是模拟
    "写到一半"的现场所依赖的前提。调用方传**文件对象**时（pandas 文档允许的
    入参形态）流是开着的，第二次写会直接接在第一次后面：两个 parquet 首尾
    相接，读出来报 ``Column cannot have more than one dictionary``。那是桩自己
    把暂存区写坏了，不是被测代码把分区写坏了——目标文件在这两次调用里一个
    字节都没被动过。

    这里对流做 flush + 截断 + 归位，把桩的模拟还原成"两次独立调用"；对路径
    入参是空操作（``to_parquet`` 本来就会截断）。
    """
    if isinstance(path, (str, os.PathLike)):
        return
    reset = getattr(path, "truncate", None)
    seek = getattr(path, "seek", None)
    flush = getattr(path, "flush", None)
    if callable(reset) and callable(seek) and callable(flush):
        flush()
        reset(0)
        seek(0)


# ---------------------------------------------------------------------------
# 组 1 · exclusive_write_exit：同分区的写入互斥必须在两个真进程之间成立
# ---------------------------------------------------------------------------

_LOCK_HOLDER_SCRIPT = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    from astock_backtester.data.filelock import CrossProcessFileLock

    lock = CrossProcessFileLock(Path(sys.argv[2]), timeout=5.0)
    lock.acquire()
    print("HELD", flush=True)
    time.sleep(float(sys.argv[3]))
    lock.release()
    print("RELEASED", flush=True)
    """
)

_EXTERNAL_WRITER_SCRIPT = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from pathlib import Path
    import pandas as pd
    from astock_backtester.data.filelock import CrossProcessFileLock

    # 外部补数脚本：持锁后等父进程放行信号，再写自己的一批行、放锁
    root = Path(sys.argv[2])
    partition = root / "warehouse" / "daily_bars" / "year=2024" / "daily_bars.parquet"
    partition.parent.mkdir(parents=True, exist_ok=True)
    dates = pd.bdate_range("2024-01-02", periods=8)
    frame = pd.DataFrame({
        "symbol": ["000001"] * 8,
        "trade_date": list(dates),
        "open": [10.0] * 8, "high": [10.5] * 8, "low": [9.8] * 8, "close": [10.2] * 8,
        "volume": [1000] * 8,
    })
    with CrossProcessFileLock(partition, timeout=5.0):
        print("HELD", flush=True)
        while not (root / "writer-go").exists():
            time.sleep(0.02)
        frame.to_parquet(partition, index=False)
        print("DONE", flush=True)
    """
)


def test_lock_held_by_another_process_blocks_a_second_writer(tmp_path):
    """子进程持锁期间，第二个写者必须拿不到锁；释放后立即可拿。

    这是互斥的最小可观察事实：伪锁（哨兵文件存在性检查）让第二个写者
    立即"成功"，两个进程同时进入临界区。
    """
    lock_target = _partition(tmp_path, 2024)
    lock_target.parent.mkdir(parents=True, exist_ok=True)

    proc = _spawn(_LOCK_HOLDER_SCRIPT, str(lock_target), "1.5")
    try:
        assert proc.stdout.readline().strip() == "HELD", proc.stderr.read()

        contender = CrossProcessFileLock(lock_target, timeout=0.3, poll_seconds=0.05)
        with pytest.raises(FileLockTimeout):
            contender.acquire()

        assert proc.stdout.readline().strip() == "RELEASED"
        proc.wait(timeout=5)
        holder = CrossProcessFileLock(lock_target, timeout=2.0, poll_seconds=0.05)
        with holder:
            assert holder._handle is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_external_writer_holding_the_lock_defers_warehouse_writes(tmp_path):
    """外部脚本持锁写同一分区：数据仓的写必须等它放锁，一行都不能丢。

    外部脚本持锁后等放行信号再写 8 行、放锁；数据仓随后写入另外 8 行。
    持锁窗口是确定的（信号握手，不靠猜时序）：窗口内第二个获取必须
    超时；数据仓的写在真互斥下排队到外部写完之后，合并结果两份都在。
    """
    proc = _spawn(_EXTERNAL_WRITER_SCRIPT, str(tmp_path))
    try:
        assert proc.stdout.readline().strip() == "HELD", proc.stderr.read()

        # 持锁窗口内必须拿不到锁（伪锁在这里会立即"成功"）
        contender = CrossProcessFileLock(_partition(tmp_path, 2024), timeout=0.5, poll_seconds=0.05)
        with pytest.raises(FileLockTimeout):
            contender.acquire()

        # 放行：外部脚本写完 8 行、放锁
        (tmp_path / "writer-go").write_text("go", encoding="utf-8")
        warehouse = Warehouse(tmp_path)
        warehouse.write_daily_bars(_bars("600519", "2024-01-02", 8))
        out, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, f"外部写入进程异常退出：{err}"
        assert "DONE" in out

        result = warehouse.read_daily_bars(start_date="2024-01-01", end_date="2024-12-31")
        per_symbol = {symbol: len(rows) for symbol, rows in result.groupby("symbol")}
        assert per_symbol.get("000001") == 8, f"外部脚本写入的行被覆盖：{per_symbol}"
        assert per_symbol.get("600519") == 8, f"数据仓写入的行丢失：{per_symbol}"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# 组 2 · atomic_replace_exit：写入全程只允许"整旧"或"整新"
# ---------------------------------------------------------------------------


def test_reader_never_sees_partial_file_during_replace(tmp_path, monkeypatch):
    """写到一半时读目标文件，读到的必须是完整的旧内容或完整的新内容。

    观察点放在数据落盘的瞬间：先落一半的行、此时读目标文件、再落完整
    内容。直写目标路径会把"半截"交给读者；走临时文件再替换的写法，
    目标文件在替换之前始终是完整的旧内容。

    判据只看"这次落盘写的是不是目标文件本身"，**不对暂存文件的命名/位置提要求**：
    临时文件叫 `x.parquet.tmp`、`x.tmp`、`x.parquet.<pid>.tmp` 还是放在别处，
    都是实现自由（2026-10-02 修：旧判据要求名字以目标全名为前缀，
    把 `path.with_suffix(".tmp")` 这类正确写法误判成红——那是假阴性）。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars("600519", "2024-01-02", 5))
    target = _partition(tmp_path, 2024)
    old_rows = pq.read_table(target).num_rows

    seen: list[int] = []
    real_to_parquet = pd.DataFrame.to_parquet

    def half_then_full(frame, path, *args, **kwargs):
        where = _written_path(path)
        if where is None or where != target:
            # 先写一半的行（磁盘上真实出现"写到一半"的现场），此时读目标
            real_to_parquet(frame.iloc[: max(1, len(frame) // 2)], path, index=False)
            seen.append(pq.read_table(target).num_rows)
            _reset_destination(path)
            real_to_parquet(frame, path, index=False)
            return
        return real_to_parquet(frame, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", half_then_full)
    warehouse.write_daily_bars(_bars("000001", "2024-02-05", 3))
    monkeypatch.undo()

    assert seen and all(count in (old_rows, old_rows + 3) for count in seen), (
        f"写入中途读到了非旧非新的内容：{seen}"
    )
    assert pq.read_table(target).num_rows == old_rows + 3


def test_failed_write_keeps_previous_partition_intact(tmp_path, monkeypatch):
    """写一半失败（磁盘写不下去）：盘上的分区必须还是写之前的完整内容。

    失败现场 = 半截内容已经落盘、随后抛错。直写目标路径时这半截就是
    分区的新内容；先写临时文件的写法里它只存在于临时文件，失败清理
    后目标分区原样未动。

    判据同样只看"写的是不是目标文件本身"，不限定暂存文件的命名（见上一条的说明）。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars("600519", "2024-01-02", 5))
    target = _partition(tmp_path, 2024)
    rows_before = pq.read_table(target).num_rows

    real_to_parquet = pd.DataFrame.to_parquet

    def failing_to_parquet(frame, path, *args, **kwargs):
        where = _written_path(path)
        if where is None or where != target:
            real_to_parquet(frame.iloc[: max(1, len(frame) // 2)], path, index=False)
            raise OSError("disk full")
        return real_to_parquet(frame, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", failing_to_parquet)
    with pytest.raises(Exception):
        warehouse.write_daily_bars(_bars("000001", "2024-02-05", 3))
    monkeypatch.undo()

    assert pq.read_table(target).num_rows == rows_before, "写失败破坏了原分区内容"
    assert list(target.parent.glob("*.tmp")) == [], "写失败后残留临时文件"


# ---------------------------------------------------------------------------
# 组 3 · corrupt_visibility_exit：坏分区必须被认出来，而不是当成没数据
# ---------------------------------------------------------------------------


def test_corrupt_partition_is_reported_not_swallowed(tmp_path):
    """存在但读不出来的分区：读侧必须响（原样抛错），且登记损坏清单。

    把坏分区当空表返回，等于告诉调用方"这一年还没采集"——补数任务会去
    重抓、再写、再读，循环里没有任何报错。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars("600519", "2024-01-02", 3))
    bad = _corrupt_partition(tmp_path, 2026)

    with pytest.raises(Exception):
        warehouse.read_daily_bars(start_date="2024-01-01", end_date="2026-12-31")

    recorded = warehouse.corrupt_partitions
    assert str(bad) in recorded, "坏分区没有进入损坏登记"
    assert recorded[str(bad)], "损坏登记没有保留原始错误信息"


def test_corrupt_partition_does_not_disguise_as_empty_in_daily_read(tmp_path):
    """第二数据场景：只有坏分区、没有任何好分区时，读侧同样必须响。

    全仓只有一个分区且它是坏的：把坏分区当空表，结果集就和"空仓"一模
    一样——损坏与未采集从此无法区分。
    """
    warehouse = Warehouse(tmp_path)
    bad = _corrupt_partition(tmp_path, 2019)

    with pytest.raises(Exception):
        warehouse.read_daily_bars(start_date="2019-01-01", end_date="2019-12-31")
    assert str(bad) in warehouse.corrupt_partitions


def test_missing_partition_still_means_no_data(tmp_path):
    """对照组：分区文件不存在 == 还没采集 == 空表，不得误报损坏。"""
    warehouse = Warehouse(tmp_path)

    frame = warehouse.read_daily_bars(start_date="2030-01-01", end_date="2030-12-31")

    assert frame.empty, "不存在的分区应读出空表"
    assert warehouse.corrupt_partitions == {}, "不存在的分区不该进损坏登记"


# ---------------------------------------------------------------------------
# 组 4 · health_exit：诊断口径必须把"损坏"与"还没写"分开报
# ---------------------------------------------------------------------------


def _start_server(tmp_path):
    """起一个真实诊断服务；返回 (server, port)。用完 shutdown。"""
    from astock_backtester.service import create_server

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def test_diagnostics_reports_corrupt_partition_when_present(tmp_path):
    """有坏分区时，诊断端点必须把分区级损坏带出去（healthy=False）。"""
    server, port = _start_server(tmp_path)
    try:
        # 损坏要登记在服务进程正在用的那个数据仓实例上
        warehouse = server.state.warehouse
        warehouse.write_daily_bars(_bars("600519", "2024-01-02", 3))
        _corrupt_partition(tmp_path, 2026)
        try:
            warehouse.read_latest_daily_bars(days=1)
        except Exception:
            pass
        assert warehouse.corrupt_partitions, "前置条件：损坏应已被登记"

        payload = _get_json(port, "/diagnostics/data-gaps", allow_error=True)
        health = payload.get("warehouse_health") or {}
        assert health.get("healthy") is False, "有坏分区却报了健康"
        corrupt = health.get("corrupt_partitions") or {}
        assert any("year=2026" in path for path in corrupt), (
            f"健康口径没有带上分区级损坏明细：{health}"
        )
    finally:
        server.shutdown()


def test_diagnostics_reports_healthy_only_when_clean(tmp_path):
    """对照组：干净数据仓报健康；"还没采集"（空仓）同样不算损坏。"""
    server, port = _start_server(tmp_path)
    try:
        server.state.warehouse.write_daily_bars(_bars("600519", "2024-01-02", 3))
        payload = _get_json(port, "/diagnostics/data-gaps", allow_error=True)
        health = payload.get("warehouse_health") or {}
        assert health.get("healthy") is True, "干净数据仓被报成不健康"
        assert not (health.get("corrupt_partitions") or {}), "干净数据仓不应有损坏明细"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# 组 5 · coherence：同一份坏分区，读、登记、健康三个口径必须互恰
# ---------------------------------------------------------------------------


def _coverage_for(warehouse: Warehouse, dataset: str):
    for item in warehouse.coverage():
        if item.dataset == dataset:
            return item
    raise AssertionError(f"覆盖汇总里没有 {dataset} 数据集")


def test_coverage_drops_to_zero_exactly_when_reads_fail(tmp_path):
    """读侧"响"与损坏登记必须互恰：一边失败一边没登记（或反过来）都是分叉。

    损坏被静默吞掉时的典型现场：读侧"岁月静好"地返回空表，登记里却
    （可能）留着一条；或者反过来。同一年分区只有一个事实——它坏了——
    两个口径必须给出同一个答案。
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars("600519", "2024-01-02", 3))
    bad = _corrupt_partition(tmp_path, 2025)

    try:
        warehouse.read_daily_bars(start_date="2024-01-01", end_date="2025-12-31")
        read_failed = False
    except Exception:
        read_failed = True
    corrupt_reported = str(bad) in warehouse.corrupt_partitions

    assert read_failed == corrupt_reported, (
        "读侧与损坏登记对同一年分区给出了两个答案："
        f"读侧{'失败' if read_failed else '成功'}，登记{'有' if corrupt_reported else '无'}"
    )

    # 好分区（2024）的行数在任何口径下都必须原样数出来（防过度纠正：
    # 不能为了报坏把好分区一起吞掉）
    frame = warehouse.read_daily_bars(start_date="2024-01-01", end_date="2024-12-31")
    assert len(frame) == 3, f"好分区的行数被连带吞掉：只剩 {len(frame)} 行"


def test_all_three_surfaces_agree_on_a_corrupt_year(tmp_path):
    """读侧、损坏登记、诊断健康对同一份坏分区只能有一个答案。

    只要有一处把"坏"吞成"没数据"、另一处如实登记，三口径就会出现
    "读得出/报健康"的自相矛盾——这正是症状里"覆盖数清零又自己恢复"
    与"页面看不到任何损坏提示"的机制。
    """
    server, port = _start_server(tmp_path)
    try:
        warehouse = server.state.warehouse
        warehouse.write_daily_bars(_bars("600519", "2024-01-02", 3))
        _corrupt_partition(tmp_path, 2025)

        try:
            warehouse.read_daily_bars(start_date="2024-01-01", end_date="2025-12-31")
            read_failed = False
        except Exception:
            read_failed = True
        corrupt_reported = str(_partition(tmp_path, 2025)) in warehouse.corrupt_partitions

        assert read_failed == corrupt_reported, (
            "读侧与损坏登记对同一年分区给出了两个答案："
            f"读侧{'失败' if read_failed else '成功'}，登记{'有' if corrupt_reported else '无'}"
        )

        payload = _get_json(port, "/diagnostics/data-gaps", allow_error=True)
        health = payload.get("warehouse_health") or {}
        corrupt_in_health = any(
            "year=2025" in path for path in (health.get("corrupt_partitions") or {})
        )
        assert health.get("healthy") is not True, "存在坏分区时诊断口径仍报健康"
        assert corrupt_in_health, "诊断口径没有认出这一年分区是坏的"
    finally:
        server.shutdown()


def test_clean_years_stay_consistent_across_all_surfaces(tmp_path):
    """对照组：没有坏分区时，三口径一致地报"正常"，覆盖数与写入行数一致。"""
    server, port = _start_server(tmp_path)
    try:
        warehouse = server.state.warehouse
        warehouse.write_daily_bars(_bars("600519", "2024-01-02", 3))
        warehouse.write_daily_bars(_bars("000001", "2024-01-02", 3))

        frame = warehouse.read_daily_bars(start_date="2024-01-01", end_date="2024-12-31")
        assert len(frame) == 6, f"读到的行数与写入不符：{len(frame)}"

        summary = _coverage_for(warehouse, "daily_bars")
        assert summary.symbols == 2, f"覆盖汇总的股票数与写入不符：{summary.symbols}"
        assert warehouse.corrupt_partitions == {}

        payload = _get_json(port, "/diagnostics/data-gaps", allow_error=True)
        health = payload.get("warehouse_health") or {}
        assert health.get("healthy") is True, "干净数据仓被诊断口径报成不健康"
    finally:
        server.shutdown()
