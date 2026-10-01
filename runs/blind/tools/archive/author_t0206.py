"""T2-06 成题脚本：从受测仓库生成注入补丁、参考解、隐藏测试与全部题包文件。

题：数据仓并发写与损坏暴露（§7 第 6 题 · 中级）。
四端口注入（均按 reference/notes.md 草案，不另行发明）：
  ① 去跨进程锁    filelock.acquire 退化成"存在性检查 + 创建"的伪锁
  ② 去原子替换    _atomic_write_parquet 直接 truncate 写目标文件
  ③ 损坏静默      _safe_read_parquet 登记后返回空表（模仿 cache.py 读侧）
  ④ 不暴露健康    /diagnostics/data-gaps 的 warehouse_health 常报"健康"
受测仓库全程只读，补丁在内存中生成。

产出（写进 packs/core/tasks/T2-06/）：
  inject/patches/0001-filelock-pseudo-lock.patch
  inject/patches/0002-warehouse-non-atomic-write.patch
  inject/patches/0003-warehouse-silent-corruption.patch
  inject/patches/0004-service-hidden-health.patch
  reference/fix.patch / partial.patch（partial 只修写路径端口①②）
  hidden/tests_hidden/test_warehouse_corruption.py + hidden/groups.json
  p2p.json、prompts/1-3.md、calibration/results.json、meta.json
（reference/notes.md 成题版由出题者在门禁跑完后单独回填，脚本只建目录。）
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T2-06"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))
sys.path.insert(0, str(ROOT / "runs" / "blind" / "tools"))

from mkpatch import build_patch  # noqa: E402
import packgate  # noqa: E402

FILELOCK_REL = "backend/astock_backtester/data/filelock.py"
WAREHOUSE_REL = "backend/astock_backtester/data/warehouse.py"
SERVICE_REL = "backend/astock_backtester/service.py"

filelock_src = (REPO / FILELOCK_REL).read_text(encoding="utf-8")
warehouse_src = (REPO / WAREHOUSE_REL).read_text(encoding="utf-8")
service_src = (REPO / SERVICE_REL).read_text(encoding="utf-8")


def cut(text: str, anchor: str) -> str:
    """取锚点起的整段（到下一个同级 def/class），替换时用作旧文本。"""
    return text[text.index(anchor) : text.index("\n    def ", text.index(anchor) + 1) + 1]


# ==========================================================================
# 一、注入变体（四端口；文本 = 原文锚点逐字拷贝）
# ==========================================================================

# ---- 端口①：filelock.acquire 伪锁化 -------------------------------------
filelock_injected = filelock_src.replace(
    '''"""跨进程文件锁 —— 数据仓写入互斥的最小实现。

为什么需要：`Warehouse.write_daily_bars` 是 read-modify-write + 整文件覆盖。
桌面端 sidecar 与外部补齐脚本同时运行时，两方交错的「读 → 合 → 写」会
**丢失更新**（后写者覆盖先写者）并可能产生 **撕裂写**（一方读到另一方
写到一半的文件 → parquet footer 与数据块不匹配 → 分区损坏）。

`threading.Lock` 只在单进程内有效，因此这里用操作系统级文件锁：
- Windows：`msvcrt.locking`（锁 1 字节区域）
- POSIX：`fcntl.flock`

锁的持有者是**文件描述符**，进程崩溃时由 OS 自动释放，不会留下死锁。
"""''',
    '''"""数据仓写入的哨兵文件协调器。

写入方对同一分区各自认领一个 ``<target>.lock`` 哨兵文件，靠它的存在性
表达"我准备写这里"：进入临界区前创建哨兵，写完保持原样（下次直接复用，
避免 unlink/reopen 竞态）。只要双方都遵守这个约定，文件系统层面就有一
个共同的落点可以核对，排查并发问题时也有了明确的现场。

保留 ``timeout`` 参数与超时异常，是为了兼容仍在等待旧锁语义的调用方；
默认等待时长保持 120 秒不变（写大分区 + 杀毒软件扫描时确实需要这么久）。
"""''',
    1)

filelock_injected = filelock_injected.replace(
    '''    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")  # noqa: SIM115 - 生命周期由 acquire/release 管理
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                if _try_lock_file(handle):
                    self._handle = handle
                    return
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(str(self.lock_path), self.timeout)
                time.sleep(self.poll_seconds)
        except BaseException:
            handle.close()
            raise
''',
    '''    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")  # noqa: SIM115 - 生命周期由 acquire/release 管理
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                if self._claim_sentinel(handle):
                    self._handle = handle
                    return
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(str(self.lock_path), self.timeout)
                time.sleep(self.poll_seconds)
        except BaseException:
            handle.close()
            raise

    @staticmethod
    def _claim_sentinel(handle) -> bool:
        """认领哨兵：文件可打开即视为拿到了协调位。

        各写入方的哨兵本来就是各自创建、各自可见的，再叠加字节级锁定
        只会让持有方之外的所有访问都排队；写入方按序落盘已经足够。
        """
        handle.seek(0)
        return not handle.read(1)
''',
    1)
assert "self._claim_sentinel(handle)" in filelock_injected, "端口① acquire 注入未命中"

# ---- 端口②：_atomic_write_parquet 直写目标 ------------------------------
warehouse_injected = warehouse_src.replace(
    '''            # 跨进程互斥：read-modify-write 必须原子化，否则桌面端 sidecar 与
            # 外部脚本并发写同一分区会丢失更新甚至撕裂文件（footer 不匹配）。
            with CrossProcessFileLock(path):''',
    '''            # 写入方先认领哨兵再落盘（filelock 模块）：桌面端 sidecar 与
            # 外部脚本各自核对协调位，避免同一分区的无序写。
            with CrossProcessFileLock(path):''',
    1)

warehouse_injected = warehouse_injected.replace(
    '''    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """临时文件写完后 ``os.replace`` 原子替换。

        ``to_parquet`` 直接写目标路径时会先 truncate 再逐块写，任何并发读取者
        （包括桌面端自己读 coverage）都可能读到半成品并报
        ``Parquet magic bytes not found in footer``。写到同目录下的临时文件再
        ``os.replace`` 保证读取者要么看到旧文件、要么看到完整新文件。
        """
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            frame.to_parquet(tmp_path, index=False)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
''',
    '''    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """落盘日线分区。

        落盘路径在写入前已被哨兵协调（见 ``write_daily_bars``），这里保持
        尽量少的文件操作：直接写目标路径，不在同目录留下任何中间产物，
        失败时也不会有半截的临时文件残留需要清理。
        """
        frame.to_parquet(path, index=False)
''',
    1)
assert "frame.to_parquet(path, index=False)" in warehouse_injected, "端口② 直写注入未命中"

# ---- 端口③：_safe_read_parquet 登记后返回空表 ---------------------------
warehouse_injected = warehouse_injected.replace(
    '''    def _safe_read_parquet(self, path: Path, **kwargs) -> pd.DataFrame:
        """Read a partition, distinguishing "absent" from "unreadable".

        A missing file legitimately means "no data yet" and yields an empty
        frame.  A file that exists but cannot be parsed means the partition is
        corrupt — silently returning an empty frame there disguises corruption
        as *missing rows*, which sends sync into a re-download loop that gets
        overwritten again next round.  The original error is re-raised
        unchanged, and the path is recorded in ``corrupt_partitions`` so the UI
        and AI tools can tell "corrupt" apart from "not collected yet".
        """
        try:
            return pd.read_parquet(path, **kwargs)
        except FileNotFoundError:  # 文件不存在 == 还没有数据
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged
            if path.exists():
                self._note_corrupt_partition(path, exc)
            raise
''',
    '''    def _safe_read_parquet(self, path: Path, **kwargs) -> pd.DataFrame:
        """Read a partition, tolerating absent and unreadable files.

        A missing file legitimately means "no data yet" and yields an empty
        frame.  A file that exists but cannot be parsed is recorded in
        ``corrupt_partitions`` with the original error, then treated the same
        way: one bad partition must never take down every reader that walks
        the directory — callers get an empty frame and the diagnostic record
        keeps the evidence for the health surface to report.
        """
        try:
            return pd.read_parquet(path, **kwargs)
        except FileNotFoundError:  # 文件不存在 == 还没有数据
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - 按分区隔离后继续
            if path.exists():
                self._note_corrupt_partition(path, exc)
            return pd.DataFrame()
''',
    1)
assert "return pd.DataFrame()\n\n    def _note_corrupt_partition" in warehouse_injected, "端口③ 静默注入未命中"

# ---- 端口④：/diagnostics/data-gaps 常报健康 ------------------------------
service_injected = service_src.replace(
    '''        if self.path == "/diagnostics/data-gaps":
            """缺口画像：停更分布 / 疑似写入失败日 / 字段尾部（warehouse 缓存，只读不触发抓取）。"""
            try:
                profile = self.server.state.warehouse.data_gap_profile()
                # 分区损坏必须与"数据缺失"分开暴露：否则损坏会被当成缺口，
                # 反复触发全量重拉（历史上正是"补了又没补上"的成因之一）。
                corrupt = getattr(self.server.state.warehouse, "corrupt_partitions", None) or {}
                self._send_json(
                    {
                        "ok": bool(profile.get("available")),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "profile": profile,
                        "warehouse_health": {
                            "corrupt_partitions": corrupt,
                            "healthy": not corrupt,
                        },
                    }
                )
            except Exception as exc:
                self.server.state.log("error", f"data gap profile failed: {exc}")
                # 画像失败时最需要 health：分区损坏正是 data_gap_profile() 抛异常的
                # 常见原因，若不在这里带上，前端只能看到 400、把损坏误判为缺失。
                corrupt = getattr(self.server.state.warehouse, "corrupt_partitions", None) or {}
                self._send_json(
                    {
                        "ok": False,
                        "code": "request_failed",
                        "message": str(exc),
                        "profile": {"available": False},
                        "warehouse_health": {
                            "corrupt_partitions": corrupt,
                            "healthy": not corrupt,
                        },
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return''',
    '''        if self.path == "/diagnostics/data-gaps":
            """缺口画像：停更分布 / 疑似写入失败日 / 字段尾部（warehouse 缓存，只读不触发抓取）。"""
            try:
                profile = self.server.state.warehouse.data_gap_profile()
                # 画像里的缺口分布已经覆盖"哪些日子少数据"；健康口径只表达
                # 这个端点自己有没有把画像算出来，不掺具体分区细节。
                self._send_json(
                    {
                        "ok": bool(profile.get("available")),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "profile": profile,
                        "warehouse_health": {"healthy": True},
                    }
                )
            except Exception as exc:
                self.server.state.log("error", f"data gap profile failed: {exc}")
                self._send_json(
                    {
                        "ok": False,
                        "code": "request_failed",
                        "message": str(exc),
                        "profile": {"available": False},
                        "warehouse_health": {"healthy": False},
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return''',
    1)
assert '"warehouse_health": {"healthy": True}' in service_injected, "端口④ 健康隐藏注入未命中"

# ==========================================================================
# 二、锚解变体（fix：四端口全修齐；partial：只修写路径端口①②）
# ==========================================================================

# ---- 锚解·端口①（filelock 回到真锁） ------------------------------------
filelock_fixed = filelock_injected.replace(
    '''"""数据仓写入的哨兵文件协调器。

写入方对同一分区各自认领一个 ``<target>.lock`` 哨兵文件，靠它的存在性
表达"我准备写这里"：进入临界区前创建哨兵，写完保持原样（下次直接复用，
避免 unlink/reopen 竞态）。只要双方都遵守这个约定，文件系统层面就有一
个共同的落点可以核对，排查并发问题时也有了明确的现场。

保留 ``timeout`` 参数与超时异常，是为了兼容仍在等待旧锁语义的调用方；
默认等待时长保持 120 秒不变（写大分区 + 杀毒软件扫描时确实需要这么久）。
"""''',
    '''"""跨进程文件锁 —— 数据仓写入互斥的最小实现。

为什么需要：`Warehouse.write_daily_bars` 是 read-modify-write + 整文件覆盖。
桌面端 sidecar 与外部补齐脚本同时运行时，两方交错的「读 → 合 → 写」会
**丢失更新**（后写者覆盖先写者）并可能产生 **撕裂写**（一方读到另一方
写到一半的文件 → parquet footer 与数据块不匹配 → 分区损坏）。

`threading.Lock` 只在单进程内有效，因此这里用操作系统级文件锁：
- Windows：`msvcrt.locking`（锁 1 字节区域）
- POSIX：`fcntl.flock`

锁的持有者是**文件描述符**，进程崩溃时由 OS 自动释放，不会留下死锁。
"""''',
    1)
filelock_fixed = filelock_fixed.replace(
    '''    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")  # noqa: SIM115 - 生命周期由 acquire/release 管理
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                if self._claim_sentinel(handle):
                    self._handle = handle
                    return
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(str(self.lock_path), self.timeout)
                time.sleep(self.poll_seconds)
        except BaseException:
            handle.close()
            raise

    @staticmethod
    def _claim_sentinel(handle) -> bool:
        """认领哨兵：文件可打开即视为拿到了协调位。

        各写入方的哨兵本来就是各自创建、各自可见的，再叠加字节级锁定
        只会让持有方之外的所有访问都排队；写入方按序落盘已经足够。
        """
        handle.seek(0)
        return not handle.read(1)
''',
    '''    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")  # noqa: SIM115 - 生命周期由 acquire/release 管理
        try:
            deadline = time.monotonic() + self.timeout
            while True:
                if _try_lock_file(handle):
                    self._handle = handle
                    return
                if time.monotonic() >= deadline:
                    raise FileLockTimeout(str(self.lock_path), self.timeout)
                time.sleep(self.poll_seconds)
        except BaseException:
            handle.close()
            raise
''',
    1)
assert "_try_lock_file(handle)" in filelock_fixed and "_claim_sentinel" not in filelock_fixed

# ---- 锚解·端口②③（warehouse：原子替换与损坏语义复原） -------------------
warehouse_fixed = warehouse_injected.replace(
    '''            # 写入方先认领哨兵再落盘（filelock 模块）：桌面端 sidecar 与
            # 外部脚本各自核对协调位，避免同一分区的无序写。
            with CrossProcessFileLock(path):''',
    '''            # 跨进程互斥：read-modify-write 必须互斥，否则桌面端 sidecar 与
            # 外部脚本并发写同一分区会丢失更新甚至撕裂文件（footer 不匹配）。
            with CrossProcessFileLock(path):''',
    1)
warehouse_fixed = warehouse_fixed.replace(
    '''    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """落盘日线分区。

        落盘路径在写入前已被哨兵协调（见 ``write_daily_bars``），这里保持
        尽量少的文件操作：直接写目标路径，不在同目录留下任何中间产物，
        失败时也不会有半截的临时文件残留需要清理。
        """
        frame.to_parquet(path, index=False)
''',
    '''    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """临时文件写完后 ``os.replace`` 原子替换。

        ``to_parquet`` 直接写目标路径时会先 truncate 再逐块写，任何并发读取者
        （包括桌面端自己读 coverage）都可能读到半成品并报
        ``Parquet magic bytes not found in footer``。写到同目录下的临时文件再
        ``os.replace`` 保证读取者要么看到旧文件、要么看到完整新文件。
        """
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            frame.to_parquet(tmp_path, index=False)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
''',
    1)
warehouse_fixed = warehouse_fixed.replace(
    '''    def _safe_read_parquet(self, path: Path, **kwargs) -> pd.DataFrame:
        """Read a partition, tolerating absent and unreadable files.

        A missing file legitimately means "no data yet" and yields an empty
        frame.  A file that exists but cannot be parsed is recorded in
        ``corrupt_partitions`` with the original error, then treated the same
        way: one bad partition must never take down every reader that walks
        the directory — callers get an empty frame and the diagnostic record
        keeps the evidence for the health surface to report.
        """
        try:
            return pd.read_parquet(path, **kwargs)
        except FileNotFoundError:  # 文件不存在 == 还没有数据
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - 按分区隔离后继续
            if path.exists():
                self._note_corrupt_partition(path, exc)
            return pd.DataFrame()
''',
    '''    def _safe_read_parquet(self, path: Path, **kwargs) -> pd.DataFrame:
        """Read a partition, distinguishing "absent" from "unreadable".

        A missing file legitimately means "no data yet" and yields an empty
        frame.  A file that exists but cannot be parsed means the partition is
        corrupt — silently returning an empty frame there disguises corruption
        as *missing rows*, which sends sync into a re-download loop that gets
        overwritten again next round.  The original error is re-raised
        unchanged, and the path is recorded in ``corrupt_partitions`` so the UI
        and AI tools can tell "corrupt" apart from "not collected yet".
        """
        try:
            return pd.read_parquet(path, **kwargs)
        except FileNotFoundError:  # 文件不存在 == 还没有数据
            return pd.DataFrame()
        except Exception as exc:  # noqa: BLE001 - re-raised unchanged
            if path.exists():
                self._note_corrupt_partition(path, exc)
            raise
''',
    1)
assert "raise\n" in warehouse_fixed[warehouse_fixed.index("def _safe_read_parquet"):]

# ---- 锚解·端口④（service：健康口径如实暴露） -----------------------------
service_fixed = service_injected.replace(
    '''            try:
                profile = self.server.state.warehouse.data_gap_profile()
                # 画像里的缺口分布已经覆盖"哪些日子少数据"；健康口径只表达
                # 这个端点自己有没有把画像算出来，不掺具体分区细节。
                self._send_json(
                    {
                        "ok": bool(profile.get("available")),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "profile": profile,
                        "warehouse_health": {"healthy": True},
                    }
                )
            except Exception as exc:
                self.server.state.log("error", f"data gap profile failed: {exc}")
                self._send_json(
                    {
                        "ok": False,
                        "code": "request_failed",
                        "message": str(exc),
                        "profile": {"available": False},
                        "warehouse_health": {"healthy": False},
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return''',
    '''            try:
                profile = self.server.state.warehouse.data_gap_profile()
                # 分区损坏必须与"数据缺失"分开暴露：否则损坏会被当成缺口，
                # 反复触发全量重拉（历史上正是"补了又没补上"的成因之一）。
                corrupt = getattr(self.server.state.warehouse, "corrupt_partitions", None) or {}
                self._send_json(
                    {
                        "ok": bool(profile.get("available")),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "profile": profile,
                        "warehouse_health": {
                            "corrupt_partitions": corrupt,
                            "healthy": not corrupt,
                        },
                    }
                )
            except Exception as exc:
                self.server.state.log("error", f"data gap profile failed: {exc}")
                # 画像失败时最需要 health：分区损坏正是 data_gap_profile() 抛异常的
                # 常见原因，若不在这里带上，前端只能看到 400、把损坏误判为缺失。
                corrupt = getattr(self.server.state.warehouse, "corrupt_partitions", None) or {}
                self._send_json(
                    {
                        "ok": False,
                        "code": "request_failed",
                        "message": str(exc),
                        "profile": {"available": False},
                        "warehouse_health": {
                            "corrupt_partitions": corrupt,
                            "healthy": not corrupt,
                        },
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return''',
    1)
assert "healthy\": not corrupt" in service_fixed

# ==========================================================================
# 三、半成品变体（只修写路径端口①②：锁 + 原子替换）
# ==========================================================================

warehouse_partial = warehouse_injected.replace(
    '''            # 写入方先认领哨兵再落盘（filelock 模块）：桌面端 sidecar 与
            # 外部脚本各自核对协调位，避免同一分区的无序写。
            with CrossProcessFileLock(path):''',
    '''            # 跨进程互斥：read-modify-write 必须互斥，否则桌面端 sidecar 与
            # 外部脚本并发写同一分区会丢失更新甚至撕裂文件（footer 不匹配）。
            with CrossProcessFileLock(path):''',
    1)
warehouse_partial = warehouse_partial.replace(
    '''    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """落盘日线分区。

        落盘路径在写入前已被哨兵协调（见 ``write_daily_bars``），这里保持
        尽量少的文件操作：直接写目标路径，不在同目录留下任何中间产物，
        失败时也不会有半截的临时文件残留需要清理。
        """
        frame.to_parquet(path, index=False)
''',
    '''    @staticmethod
    def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
        """临时文件写完后 ``os.replace`` 原子替换。

        ``to_parquet`` 直接写目标路径时会先 truncate 再逐块写，任何并发读取者
        （包括桌面端自己读 coverage）都可能读到半成品并报
        ``Parquet magic bytes not found in footer``。写到同目录下的临时文件再
        ``os.replace`` 保证读取者要么看到旧文件、要么看到完整新文件。
        """
        tmp_path = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            frame.to_parquet(tmp_path, index=False)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
''',
    1)
assert "os.replace(tmp_path, path)" in warehouse_partial

# ==========================================================================
# 四、补丁产出
# ==========================================================================

INJECT_DIR = TASK / "inject" / "patches"
REFERENCE = TASK / "reference"
HIDDEN = TASK / "hidden" / "tests_hidden"
PROMPTS = TASK / "prompts"
CALIB = TASK / "calibration"
for folder in (INJECT_DIR, REFERENCE, HIDDEN, PROMPTS, CALIB):
    folder.mkdir(parents=True, exist_ok=True)


def patch(rel: str, before: str, after: str) -> str:
    return build_patch(rel, before.splitlines(keepends=True), after.splitlines(keepends=True))


def _slice(text: str, start_anchor: str, end_anchor: str) -> str:
    """取两个锚点之间的文本段（含起点、不含终点）。"""
    return text[text.index(start_anchor):text.index(end_anchor, text.index(start_anchor) + 1)]


# warehouse.py 被端口②③各改一段。apply_patch 按 hunk 头的绝对行号定位，
# 同一文件的多个补丁必须**链式**生成：0002 基于原文，0003 基于 0002 的结果。
_W_LOCK_BEFORE = _slice(warehouse_src, "            # 跨进程互斥", "            with CrossProcessFileLock(path):")
_W_LOCK_AFTER = _slice(warehouse_injected, "            # 写入方先认领哨兵", "            with CrossProcessFileLock(path):")
_W_ATOMIC_BEFORE = _slice(warehouse_src, "    @staticmethod", "    def invalidate_gap_profile")
_W_ATOMIC_AFTER = _slice(warehouse_injected, "    @staticmethod", "    def invalidate_gap_profile")
_W_SAFE_BEFORE = _slice(warehouse_src, "    def _safe_read_parquet", "    def _note_corrupt_partition")
_W_SAFE_AFTER = _slice(warehouse_injected, "    def _safe_read_parquet", "    def _note_corrupt_partition")

# 补丁②版本：锁注释改写 + 直写目标（端口②）
warehouse_patch2 = warehouse_src.replace(_W_LOCK_BEFORE, _W_LOCK_AFTER, 1).replace(
    _W_ATOMIC_BEFORE, _W_ATOMIC_AFTER, 1)
# 补丁③版本：在②的基础上再叠加损坏静默（端口③）
warehouse_patch3 = warehouse_patch2.replace(_W_SAFE_BEFORE, _W_SAFE_AFTER, 1)
assert warehouse_patch3 == warehouse_injected, "链式拼接结果与注入全量不一致"

(INJECT_DIR / "0001-filelock-pseudo-lock.patch").write_text(
    patch(FILELOCK_REL, filelock_src, filelock_injected), encoding="utf-8", newline="\n")
(INJECT_DIR / "0002-warehouse-non-atomic-write.patch").write_text(
    patch(WAREHOUSE_REL, warehouse_src, warehouse_patch2), encoding="utf-8", newline="\n")
(INJECT_DIR / "0003-warehouse-silent-corruption.patch").write_text(
    patch(WAREHOUSE_REL, warehouse_patch2, warehouse_patch3), encoding="utf-8", newline="\n")
(INJECT_DIR / "0004-service-hidden-health.patch").write_text(
    patch(SERVICE_REL, service_src, service_injected), encoding="utf-8", newline="\n")

(REFERENCE / "fix.patch").write_text("".join([
    patch(FILELOCK_REL, filelock_injected, filelock_fixed),
    patch(WAREHOUSE_REL, warehouse_injected, warehouse_fixed),
    patch(SERVICE_REL, service_injected, service_fixed),
]), encoding="utf-8", newline="\n")

(REFERENCE / "partial.patch").write_text("".join([
    patch(FILELOCK_REL, filelock_injected, filelock_fixed),
    patch(WAREHOUSE_REL, warehouse_injected, warehouse_partial),
]), encoding="utf-8", newline="\n")
print("补丁已生成（inject×4 / fix / partial）")

# ==========================================================================
# 五、隐藏测试：真双进程并发 + 原子替换 + 损坏登记 + 健康暴露 + coherence
# ==========================================================================

(HIDDEN / "test_warehouse_corruption.py").write_text(r'''"""T2-06 隐藏测试：并发写仓与损坏暴露。

只断言外部可观察的行为：互斥在两个真进程之间是否成立、落盘中途读者看到
什么、坏分区在读路径与诊断口径上的呈现。不约束实现住在哪个模块、用什么
原语。

进程级用例的确定性手法沿用仓库既有测试的双进程模式：子进程 stdout 做
消息握手（父进程等到"HELD"才开始动作），不依赖 sleep 时序，不碰网络。
"""

from __future__ import annotations

import json
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
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars("600519", "2024-01-02", 5))
    target = _partition(tmp_path, 2024)
    old_rows = pq.read_table(target).num_rows

    seen: list[int] = []
    real_to_parquet = pd.DataFrame.to_parquet

    def half_then_full(frame, path, *args, **kwargs):
        where = Path(str(path))
        if where.parent == target.parent and where.name.startswith(target.name):
            # 先写一半的行（磁盘上真实出现"写到一半"的现场），此时读目标
            real_to_parquet(frame.iloc[: max(1, len(frame) // 2)], path, index=False)
            seen.append(pq.read_table(target).num_rows)
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
    """
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(_bars("600519", "2024-01-02", 5))
    target = _partition(tmp_path, 2024)
    rows_before = pq.read_table(target).num_rows

    real_to_parquet = pd.DataFrame.to_parquet

    def failing_to_parquet(frame, path, *args, **kwargs):
        where = Path(str(path))
        if where.parent == target.parent and where.name.startswith(target.name):
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
''', encoding="utf-8", newline="\n")

# ==========================================================================
# 六、分组
# ==========================================================================

HIDDEN_TEST_IDS = "hidden/tests_hidden/test_warehouse_corruption.py::"
(TASK / "hidden" / "groups.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-06",
    "note": (
        "组 = 一个端口（附录 B 结构），四端口 + coherence + p2p 回归。"
        "coherence 权重最高：断言同一年分区在读、覆盖数、诊断健康三个口径上互相印证——"
        "损坏被静默吞掉时，三口径必然出现『读得出/数不出』或『数不出/报健康』的自相矛盾。"
        "p2p 白名单见任务根 p2p.json，任一条红 → 本轮作废（0 分）。"
    ),
    "groups": [
        {
            "id": "exclusive_write_exit",
            "weight": 1,
            "port": "跨进程互斥：两个真进程对同一分区，第二个写者必须被挡住",
            "tests": [
                HIDDEN_TEST_IDS + "test_lock_held_by_another_process_blocks_a_second_writer",
                HIDDEN_TEST_IDS + "test_external_writer_holding_the_lock_defers_warehouse_writes",
            ],
        },
        {
            "id": "atomic_replace_exit",
            "weight": 1,
            "port": "原子替换：任何时刻盘上是整旧或整新，写失败不破坏旧分区",
            "tests": [
                HIDDEN_TEST_IDS + "test_reader_never_sees_partial_file_during_replace",
                HIDDEN_TEST_IDS + "test_failed_write_keeps_previous_partition_intact",
            ],
        },
        {
            "id": "corrupt_visibility_exit",
            "weight": 1,
            "port": "损坏可见：坏分区读侧必须响并登记，缺失仍是空表",
            "tests": [
                HIDDEN_TEST_IDS + "test_corrupt_partition_is_reported_not_swallowed",
                HIDDEN_TEST_IDS + "test_corrupt_partition_does_not_disguise_as_empty_in_daily_read",
                HIDDEN_TEST_IDS + "test_missing_partition_still_means_no_data",
            ],
        },
        {
            "id": "health_exit",
            "weight": 1,
            "port": "健康暴露：诊断口径必须把损坏与未采集分开报",
            "tests": [
                HIDDEN_TEST_IDS + "test_diagnostics_reports_corrupt_partition_when_present",
                HIDDEN_TEST_IDS + "test_diagnostics_reports_healthy_only_when_clean",
            ],
        },
        {
            "id": "coherence",
            "weight": 2,
            "port": "跨口径一致：同一分区的读、覆盖数、健康三处答案互恰",
            "tests": [
                HIDDEN_TEST_IDS + "test_coverage_drops_to_zero_exactly_when_reads_fail",
                HIDDEN_TEST_IDS + "test_all_three_surfaces_agree_on_a_corrupt_year",
                HIDDEN_TEST_IDS + "test_clean_years_stay_consistent_across_all_surfaces",
            ],
        },
        {
            "id": "p2p",
            "weight": 0,
            "mode": "regression",
            "note": "既有用例白名单见任务根 p2p.json。任一条红 → 本轮作废（0 分）。",
        },
    ],
}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

# ==========================================================================
# 七、meta.json 成题版
# ==========================================================================

meta = json.loads((TASK / "meta.json").read_text(encoding="utf-8"))
meta.pop("status", None)
meta["allowed_paths"] = [
    "backend/astock_backtester/data/warehouse.py",
    "backend/astock_backtester/data/filelock.py",
    "backend/astock_backtester/service.py",
]
meta["forbidden_paths"] = [
    "tests/**",
    "pyproject.toml",
    "**/conftest.py",
    "scripts/**",
    "backend/astock_backtester/data/sync.py",
    "frontend/**",
    "packs/**",
    "console/**",
]
# 可见测试裁剪（visible.prune）：
# 1) tests/test_data_warehouse_concurrency.py 整文件——13 条用例全部是本题四端口
#    （锁 / 原子写 / 损坏登记 / 健康暴露）的守卫用例，文件名与 docstring 直接点名
#    答案；注入态实测 6 红 7 绿，绿的 7 条同样是名字点名（filelock/atomic_write/
#    diagnostics_health）。整文件裁剪还有一个工程理由：harness 的
#    snapshot.apply_prune 对同一文件逐条改写后，guess_text 以 raw[:4096] 截断判
#    文本，截断点落在多字节 UTF-8 字符中间时会把纯文本误判为二进制、静默跳过
#    后续条目（本机实测复现）；整文件删除不走该路径，行为确定。
#    run_full_market 攒批用例属 T3-10 地盘，一并移出本题沙箱（见 notes.md 分工边界）。
# 2) test_warehouse.py 的 3 条注入态实测变红 + 1 条名字点名"坏分区跳过并登记"
#    （T1-02 对同名用例同样裁剪）；该文件条目数 ≤3，不触发上述 guess_text 缺陷。
PRUNE_LIST = [
    "tests/test_data_warehouse_concurrency.py",
    "tests/test_warehouse.py::test_warehouse_safe_read_parquet_only_treats_missing_files_as_empty",
    "tests/test_warehouse.py::test_warehouse_surfaces_corrupt_recent_partition_for_latest_and_coverage",
    "tests/test_warehouse.py::test_warehouse_does_not_overwrite_corrupt_partition_when_new_rows_arrive",
    "tests/test_warehouse.py::test_market_trade_date_counts_skips_corrupt_partition",
]
meta["visible"] = {"prune": PRUNE_LIST}
meta["redactions"] = [
    {"file": "AGENTS.md", "sections": ["9"]},
    {"file": "CHANGELOG.md", "versions": ["1.5.2", "1.6.0", "1.6.1"]},
]
meta["checks"] = [
    {"kind": "pytest", "hidden": "hidden/tests_hidden", "groups": "hidden/groups.json", "p2p": "p2p.json"},
]
meta["calibration"] = {"target_band": [0.25, 0.55], "calibrated": False}
(TASK / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                encoding="utf-8", newline="\n")

# ==========================================================================
# 八、三级提示词
# ==========================================================================

(PROMPTS / "1.md").write_text('''你面前有一个独立的代码仓库副本，工作目录就是当前目录（Windows 下显示为 Q:\\，
它是唯一允许操作的位置，不要访问该盘之外的任何路径）。
请只在这个目录内工作；完成后告诉我你改了哪些文件即可，不要执行 git commit。

## 我遇到的问题

我这边是桌面应用和一个外部补数脚本**同时**往本地行情数据仓里写数据，最近夜间跑批之后问题不断：

1. **分区读不出来**。某一年份的行情分区一查就报"文件损坏"，手动看文件发现里面是半截数据；有的年份干脆整年读不出，报损坏。昨夜明明两个程序都在往同一年里写。
2. **覆盖数清零又自己恢复**。数据健康页面上某几年的覆盖率一夜之间归零，像是统计过程被带偏了；第二天再看又恢复了，找不出规律。
3. **最不安的是有些损坏不报错**。读的时候悄悄跳过、返回空数据，页面上一片岁月静好，等对账的时候才发现少了一大块。补数任务也跟着空转：把坏掉的年份当成"还没采集"重新抓、重新写、再读——每一轮都没有任何报错。
4. 诊断接口上只看得到缺口分布，**看不到任何"分区坏了"的提示**。前端把"坏"和"还没写"当成一回事。

## 验收要求

修好之后，下面几条必须同时成立：

- 两个进程同时写同一个分区，谁也不能把对方写的内容弄丢或写坏——每一行写进去的数据都必须在；
- 任何时刻盘上的分区文件要么是旧的完整内容、要么是新的完整内容，绝不能出现半截；写一半失败也不能毁掉原来的分区；
- 分区一旦损坏，必须**响**：读侧不能把它伪装成"没数据"返回空表，健康口径必须把"损坏"和"还没写"分开报出来——哪个分区、什么错误，要看得见；
- 没写过的分区仍然是"还没有数据"的正常语义，不能为了上面的要求把缺失也当成损坏。

我不要求你改测试，也不需要新增功能。请把根因修掉，而不是在症状出现的地方打补丁。
''', encoding="utf-8", newline="\n")

(PROMPTS / "2.md").write_text('''（第 2 级提示词——不一致清单）

把"写一个分区"这件事从头到尾走一遍，会发现它同时踩着四道各自独立的防线，每一道现在都有问题：

1. **写入互斥**。写入方对同一分区的"读 → 合 → 写"本该互相排斥；现在的实现里，两个进程各自打开一个同目录的小哨兵文件就算完事，哨兵文件本身**并不互斥**——它更像一块公告板，而不是一把锁。于是两个进程可以同时进入各自的临界区。
2. **落盘方式**。分区文件本该先写完整临时文件再一步换过去；现在的实现是**直接写目标文件**——先截断、再逐块写。任何并发读取者（包括应用自己算覆盖数）都可能撞上"截断了、还没写完"的窗口，读到半截文件。
3. **读侧对坏文件的处理**。读不出来的分区，本该把原始错误**原样抛出去**、并把分区记进损坏清单；现在的实现是记完清单后**返回空表**——对调用方来说，这和"这个分区还没采集过"完全无法区分。补数任务于是把它当缺失去重抓，抓回来一写，又把坏文件盖掉，下一轮再读又空——循环里没有任何报错。
4. **健康口径**。诊断接口本该把损坏分区连同错误信息一起带出去；现在的实现只报一个笼统的"健康/不健康"，而且**损坏明细被拿掉了**——只要端点自己算完画像就报健康。前端永远看不到"哪个分区坏了"。

这四道防线单独看每一处都"像是有自己的道理"，合在一起就是现在这幅景象：写会互相踩、落盘有半截窗口、坏文件被当缺失、健康口径看不出任何异常。

另外提醒一句：数据仓读缓存那套"读侧失败降级为空表 + 记日志"的做法，在它自己的场景里是对的（缓存坏了可以回源重抓）；但数据仓分区**没有"回源"这一说**——把同一个模式搬过来，坏分区就被永久当成了缺失。
''', encoding="utf-8", newline="\n")

(PROMPTS / "3.md").write_text('''（第 3 级提示词——不变量 + 否决项）

必须同时成立的表述：

1. **同分区的写入必须互斥，且互斥必须在操作系统层面成立。** 进程内的锁对象管不到另一个进程；"哨兵文件存在"也不是互斥——两个进程可以同时观察到它不存在、同时创建、同时进入临界区。文件锁（字节区间锁定）或等价的 OS 原语才是合格答案。锁的持有者必须是进程崩溃时能被操作系统自动释放的东西。
2. **分区文件对读者只有两种合法状态：完整的旧内容，或完整的新内容。** 落盘必须走"同目录临时文件 + 原子替换"（或等价机制），替换是原子的、不可分割的观察点；写失败的任何时刻，盘上仍必须是写之前的完整内容，临时文件要清理。
3. **"文件不存在"与"文件存在但读不出来"是两个语义，读侧必须区分。** 前者是"还没有数据"，返回空表；后者是损坏，必须把原始错误原样抛出、并把分区路径连同错误登记进损坏清单——不允许吞成空表，也不允许在缺列、行组裁剪等其它路径上把两类混同。
4. **健康口径必须由损坏清单驱动。** 诊断输出里要有分区级的损坏明细（路径 + 错误）与总体健康位；没有损坏登记时报健康、报空明细。覆盖数、读侧、健康三处对同一年分区的答案必须互相印证——不允许出现"读侧当缺失、健康报正常"或"覆盖数归零却说不出为什么"的组合。

已被否决的思路（不要重提）：

- "把锁超时从 120 秒改小（比如 1 秒）"——超时抛错是**正确行为**：它意味着互斥真的在排队。改小只会让正常的大分区写入更频繁地被误判成超时，互斥本身一点没变。
- "把损坏分区当空表返回，让上层无感知地继续"——这正是病灶本身。缓存文件坏了可以回源重抓，数据仓分区没有回源；空表把"坏"伪装成"没写"，补数循环从此空转，而且每一轮都不报错。
- "在诊断接口上按画像端点自己的成败来定健康"——端点算完画像 ≠ 分区都是好的。健康位必须来自损坏登记，否则"分区坏了"这件事永远到不了页面上。

## 关于可见的测试

仓库里有一批守着写入互斥与损坏暴露的既有用例，它们的名字和说明把上面这些不变量写得很直白。你不需要去改测试、也不需要以"让某条既有用例变绿"为目标——评测用的是一套独立的、你们看不到的用例，其中并发相关的用例是真的用两个进程跑的。
''', encoding="utf-8", newline="\n")

# ==========================================================================
# 九、校准表（盲测由非出题模型填写）
# ==========================================================================

(CALIB / "results.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-06",
    "calibrated": False,
    "target_band": [0.25, 0.55],
    "owner": "author",
    "policy": "§6.4 硬纪律：出题模型不得给自己出的题做校准。本表在盲测完成前保持空表，calibrated 恒为 false。",
    "gate": {
        "note": "出题侧门禁（§5.3）由 runs/blind/tools/packgate.py 跑，原始输出见 gate_*.json。这些不是校准数据，不参与 pass@1 统计。",
        "anchor_solution": "gate_fixed.json",
        "partial_solution": "gate_partial.json",
        "injected_state": "gate_injected_x20.json",
    },
    "blind_runs": {
        "note": "每一行 = 一次『只给第 1 级提示词』的完整作答。由非出题模型填写。",
        "columns": ["run_id", "model", "tier", "prompt_level", "pass@1", "score",
                    "failed_groups", "p2p_broken", "notes"],
        "rows": [],
    },
    "summary": {"runs": 0, "pass_at_1": None, "confidence_interval": None,
                "in_band": None, "conclusion": None},
}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

# ==========================================================================
# 十、p2p 候选收集（在 packgate 造的、已按 meta 裁剪的基线树上 --collect-only -q）
# ==========================================================================

# 并发守卫文件已整文件裁剪，p2p 候选只来自 test_warehouse.py（基线全绿 54 条，
# 裁掉 4 条守卫后剩 50 条）。
CANDIDATE_FILES = [
    "tests/test_warehouse.py",
]

baseline_tree = packgate.GATES / "T2-06-collect"
if baseline_tree.exists():
    import shutil
    shutil.rmtree(baseline_tree, ignore_errors=True)
meta_now = json.loads((TASK / "meta.json").read_text(encoding="utf-8"))
meta_now["pack_dir"] = str(TASK)
packgate.build_tree("T2-06", meta_now, baseline_tree, [])

done = subprocess.run(
    [sys.executable, "-m", "pytest", *CANDIDATE_FILES, "--collect-only", "-q",
     "-p", "no:cacheprovider"],
    cwd=baseline_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
    timeout=300,
)
candidates = sorted({
    line.strip() for line in done.stdout.splitlines()
    if line.strip().startswith("tests/") and "::" in line.strip()
})
# 被裁剪的守卫用例不应出现在候选里（collect 树已按 meta 裁剪）；若有残留则报错
pruned_patterns = {entry.split("::", 1)[1] for entry in PRUNE_LIST if "::" in entry}
residue = [test for test in candidates if test.split("::", 1)[1] in pruned_patterns]
assert not residue, f"被裁剪的用例仍被收集到：{residue}"
(TASK / "p2p.json").write_text(json.dumps({
    "schema": 1,
    "task": "T2-06",
    "note": (
        "基线（未注入）全绿的既有用例（仅 test_warehouse.py）。"
        "并发守卫文件 test_data_warehouse_concurrency.py 整文件进 visible.prune（13 条全部点名本题四端口，"
        "其中 6 条注入态实测变红）；test_warehouse.py 另裁 4 条守卫用例。"
        "静默吞异常守卫（test_no_silent_swallow.py）注入态实测仍绿，保留不裁。清单与理由见 reference/notes.md。"
    ),
    "tests": candidates,
}, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
print(f"p2p 收集 {len(candidates)} 条（守卫用例已全部裁剪移出）")

# ==========================================================================
# 十一、注入态探针：沙箱内可见测试必须 0 红（§6.5）
# ==========================================================================

probe_tree = packgate.GATES / "T2-06-probe"
if probe_tree.exists():
    import shutil as _shutil
    _shutil.rmtree(probe_tree, ignore_errors=True)
packgate.build_tree("T2-06", meta_now, probe_tree, sorted((TASK / "inject" / "patches").glob("*.patch")))
for rel in ("tests/test_warehouse.py", "tests/test_no_silent_swallow.py"):
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", rel, "-p", "no:cacheprovider", "--tb=line", "-q"],
        cwd=probe_tree, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=600,
    )
    failed = [line.split(" ")[1].strip() for line in probe.stdout.splitlines()
              if line.startswith("FAILED ") or line.startswith("ERROR ")]
    assert probe.returncode == 0, f"注入态沙箱里 {rel} 出现可见红测试：{failed}\n{probe.stdout[-2000:]}"
    print(f"探针通过：{rel} 注入态 0 红")
# ==========================================================================
# 十二、终检：补丁触碰面、禁词、交付物齐全
# ==========================================================================

import re  # noqa: E402  终检用

for name in ("fix.patch", "partial.patch"):
    text = (REFERENCE / name).read_text(encoding="utf-8")
    files = sorted(set(re.findall(r"^diff --git a/(\S+) b/\S+$", text, re.M)))
    allowed = set(meta["allowed_paths"])
    outside = [f for f in files if f not in allowed]
    assert not outside, f"{name} 触碰了 allowed_paths 之外的文件：{outside}"
    print(f"{name} 触碰：{files}")

_BANNED = ["D:\\New project 6", "D:/New project 6", "醍醐测试"]
_hits = []
for path in TASK.rglob("*"):
    if path.is_file() and "__pycache__" not in path.parts:
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in _BANNED:
            if needle in text:
                _hits.append((str(path.relative_to(TASK)), needle))
assert not _hits, f"题包里出现了禁词：{_hits}"
print("禁词终检：无命中")

_REQUIRED = [
    "inject/patches/0001-filelock-pseudo-lock.patch",
    "inject/patches/0002-warehouse-non-atomic-write.patch",
    "inject/patches/0003-warehouse-silent-corruption.patch",
    "inject/patches/0004-service-hidden-health.patch",
    "hidden/tests_hidden/test_warehouse_corruption.py",
    "hidden/groups.json",
    "p2p.json",
    "prompts/1.md", "prompts/2.md", "prompts/3.md",
    "reference/fix.patch", "reference/partial.patch",
    "calibration/results.json", "meta.json",
]
for rel in _REQUIRED:
    assert (TASK / rel).is_file(), f"交付物缺失：{rel}"
print("交付物齐全（notes.md 成题版与 gate_*.json 由出题者随后落盘）")
print("T2-06 成题脚本完成；下一步：三门禁（fixed / partial / injected×20）")
