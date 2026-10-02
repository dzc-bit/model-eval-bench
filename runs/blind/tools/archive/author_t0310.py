"""T3-10 成题脚本（分片 A：注入 + 锚解 + 补丁）。

产出（写进 packs/core/tasks/T3-10/）：
  inject/patches/0001-scripts-batch-degrade.patch
  inject/patches/0002-scripts-reread-amplify.patch
  inject/patches/0003-service-health-write-side-mute.patch
  inject/patches/0004-warehouse-lightweight-coupling.patch
  reference/fix.patch      四端口全修齐
  reference/partial.patch  只修脚本侧端口（演示"单进程全绿但进程级组必红"）

用法：python runs\\blind\\tools\\author_t0310.py
（幂等：可重复运行，全部产物在内存中生成后落盘）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(r"D:\new model test")
REPO = Path(r"D:\New project 6")
TASK = ROOT / "packs" / "core" / "tasks" / "T3-10"
sys.path.insert(0, str(ROOT / "packs" / "core" / "tools"))

from mkpatch import build_patch  # noqa: E402

# 端口文件（受测仓库相对路径）
IMPORT_REL = "scripts/run-full-market-import.py"
BACKFILL_REL = "scripts/run-capital-flow-backfill.py"
SERVICE_REL = "backend/astock_backtester/service.py"
WAREHOUSE_REL = "backend/astock_backtester/data/warehouse.py"

ORIG = {
    rel: (REPO / rel).read_text(encoding="utf-8")
    for rel in (IMPORT_REL, BACKFILL_REL, SERVICE_REL, WAREHOUSE_REL)
}


def diff(rel: str, original: str, current: str) -> str:
    return build_patch(rel, original.splitlines(keepends=True), current.splitlines(keepends=True)) + "\n"


# ==========================================================================
# 端口 ①：攒批退化（run-full-market-import.py）
#   flush_batch 从"整批 concat 后一次性 write_daily_bars"改成"逐票立刻写"；
#   --write-batch-size 默认值 25 -> 1（注释同步改写为"逐票清理"口径）。
# 端口 ②：读改写放大（run-full-market-import.py + run-capital-flow-backfill.py）
#   flush 前后各做一次全分区重读合并（脚本侧再发明一次 read-modify-write；
#   warehouse 内部的锁/原子写保留，属"轻度配合"）。
# 端口 ③：服务健康写入侧哑火（service.py health_payload + sync 路由的 force）
#   健康快照不再包含写入侧状况（覆盖口径 + 新鲜度），/health 只回静态身份。
# 端口 ④：进程级并发必红（warehouse.py 轻配合：不失效计数缓存）
# ==========================================================================

# ---- 端口①②：run-full-market-import.py ------------------------------------

IMPORT_INJ = ORIG[IMPORT_REL]

# ①-a 默认批大小 25 -> 1
IMPORT_INJ2 = IMPORT_INJ.replace(
    'parser.add_argument("--write-batch-size", type=int, default=25)',
    'parser.add_argument("--write-batch-size", type=int, default=1)',
    1,
)
assert IMPORT_INJ2 != IMPORT_INJ, "import: 批大小锚点未命中"

# ①-b flush_batch 逐票写 + ② flush 前后整分区重读
OLD_FLUSH = '''    def flush_batch() -> None:
        nonlocal imported_rows
        if not batch:
            return
        warehouse.write_daily_bars(pd.concat([item[1] for item in batch], ignore_index=True))
        for symbol, _frame, source, seconds, rows, index in batch:'''

NEW_FLUSH = '''    def flush_batch() -> None:
        nonlocal imported_rows
        if not batch:
            return
        # 每只票到手即落盘，避免批次间数据在内存里积压；批次边界交给上面的
        # --write-batch-size 控制（默认逐票）。
        for symbol, frame, source, seconds, rows, index in batch:
            # 写入前把该分区已有内容整段读回来合并，保证相邻批次之间不互相
            # 覆盖（分区一旦落盘就以仓库现场为准）。
            partition_paths = warehouse._partition_paths_for_range(
                frame["trade_date"].min().strftime("%Y-%m-%d"),
                frame["trade_date"].max().strftime("%Y-%m-%d"),
            )
            merged = frame
            for partition in partition_paths:
                if partition.exists():
                    existing = warehouse._safe_read_parquet(partition)
                    if not existing.empty:
                        merged = pd.concat([existing, merged], ignore_index=True)
            warehouse.write_daily_bars(merged)
            # 写入后再整段回读一次，把落盘现场与内存批次对齐。
            for partition in partition_paths:
                if partition.exists():
                    warehouse._safe_read_parquet(partition)
        for symbol, _frame, source, seconds, rows, index in batch:'''
assert OLD_FLUSH in IMPORT_INJ2, "import: flush_batch 锚点未命中"
IMPORT_INJ2 = IMPORT_INJ2.replace(OLD_FLUSH, NEW_FLUSH, 1)

# ② 追加：进度事件按票记账（保留原逐票事件口径，不动）
IMPORT_ORIG = ORIG[IMPORT_REL]

# ---- 端口①②：run-capital-flow-backfill.py ---------------------------------
BACKFILL_INJ = ORIG[BACKFILL_REL]
BACKFILL_INJ2 = BACKFILL_INJ  # 该文件本轮不改动（放大落在 import 脚本侧）

# ---- 端口③：service.py health_payload 写入侧哑火 ---------------------------
SERVICE_INJ = ORIG[SERVICE_REL]
OLD_HEALTH = '''    def health_payload(self) -> ServiceHealth:
        refresh_finished = self.start_coverage_refresh()
        if refresh_finished is not None:
            refresh_finished.wait(HEALTH_COVERAGE_WAIT_SECONDS)
        with self._coverage_lock:
            coverage_refreshing = self._coverage_refreshing
        return ServiceHealth(
            **self.identity_payload(),
            coverage=self.coverage_snapshot(),
            coverage_refreshing=coverage_refreshing,
        )'''
NEW_HEALTH = '''    def health_payload(self) -> ServiceHealth:
        # 健康检查保持轻量：只回报进程身份与"服务在跑"，不在这里触发或等待
        # 任何覆盖刷新（覆盖数据的刷新由后台定时链路自己推进）。
        return ServiceHealth(
            **self.identity_payload(),
            coverage=[],
            coverage_refreshing=False,
        )'''
assert OLD_HEALTH in SERVICE_INJ, "service: health_payload 锚点未命中"
SERVICE_INJ2 = SERVICE_INJ.replace(OLD_HEALTH, NEW_HEALTH, 1)

# ③-b 写入后不再置脏（写入侧状况不再进入刷新链路）
for old_line, new_line in (
    ("                self.server.state.set_coverage_snapshot(result.coverage)\n", ""),
    ("                self.server.state.start_coverage_refresh(force=True)\n", ""),
):
    count = SERVICE_INJ2.count(old_line)
    assert count == 3, f"service: 写入后置脏锚点计数异常 {count}"
    SERVICE_INJ2 = SERVICE_INJ2.replace(old_line, new_line)

# ---- 端口④：warehouse.py 轻配合（不失效计数缓存） --------------------------
WAREHOUSE_INJ = ORIG[WAREHOUSE_REL]
OLD_INV = '''    def invalidate_gap_profile(self) -> None:
        """写入后丢弃缺口画像、股票池计数与每交易日行数缓存，让下一次读取反映最新数据。"""
        with self._gap_profile_lock:
            self._gap_profile_cache = None
        with self._symbol_count_lock:
            self._symbol_count_cache = None
        with self._trade_date_counts_lock:
            self._trade_date_counts_cache = None'''
NEW_INV = '''    def invalidate_gap_profile(self) -> None:
        """写入后丢弃缺口画像缓存，让下一次读取反映最新数据。

        股票池计数与每交易日行数缓存的更新交给各自的 TTL（10 分钟）自然过期：
        写入路径不再顺带清空它们，避免每次落盘都把全局统计缓存打穿、逼迫
        后续读路径反复重算。
        """
        with self._gap_profile_lock:
            self._gap_profile_cache = None'''
assert OLD_INV in WAREHOUSE_INJ, "warehouse: invalidate 锚点未命中"
WAREHOUSE_INJ2 = WAREHOUSE_INJ.replace(OLD_INV, NEW_INV, 1)

# ==========================================================================
# 锚解（fix）：四处全修
# ==========================================================================

IMPORT_FIX = ORIG[IMPORT_REL]
BACKFILL_FIX = ORIG[BACKFILL_REL]
SERVICE_FIX = ORIG[SERVICE_REL]
WAREHOUSE_FIX = ORIG[WAREHOUSE_REL]

INJECTED = {
    IMPORT_REL: IMPORT_INJ2,
    BACKFILL_REL: BACKFILL_INJ2,
    SERVICE_REL: SERVICE_INJ2,
    WAREHOUSE_REL: WAREHOUSE_INJ2,
}
FIXED = {
    IMPORT_REL: IMPORT_FIX,
    BACKFILL_REL: BACKFILL_FIX,
    SERVICE_REL: SERVICE_FIX,
    WAREHOUSE_REL: WAREHOUSE_FIX,
}

INJECT_DIR = TASK / "inject" / "patches"
REFERENCE = TASK / "reference"
INJECT_DIR.mkdir(parents=True, exist_ok=True)
REFERENCE.mkdir(parents=True, exist_ok=True)

# 注入补丁（每文件一个，按序编号）
(INJECT_DIR / "0001-import-batch-degrade.patch").write_text(
    diff(IMPORT_REL, IMPORT_ORIG, IMPORT_INJ2), encoding="utf-8")
(INJECT_DIR / "0002-backfill-reread-amplify.patch").write_text(
    diff(BACKFILL_REL, BACKFILL_INJ, BACKFILL_INJ2), encoding="utf-8")
(INJECT_DIR / "0003-service-health-write-side-mute.patch").write_text(
    diff(SERVICE_REL, ORIG[SERVICE_REL], SERVICE_INJ2), encoding="utf-8")
(INJECT_DIR / "0004-warehouse-lightweight-coupling.patch").write_text(
    diff(WAREHOUSE_REL, WAREHOUSE_INJ, WAREHOUSE_INJ2), encoding="utf-8")

# 锚解：注入态 -> 原始基线
fix_parts = []
for rel in (IMPORT_REL, BACKFILL_REL, SERVICE_REL, WAREHOUSE_REL):
    if INJECTED[rel] != FIXED[rel]:
        fix_parts.append(diff(rel, INJECTED[rel], FIXED[rel]))
(REFERENCE / "fix.patch").write_text("".join(fix_parts), encoding="utf-8")

# 半成品：只修脚本侧（import 端口①②），service 与 warehouse 不修
partial_parts = [diff(IMPORT_REL, INJECTED[IMPORT_REL], FIXED[IMPORT_REL])]
(REFERENCE / "partial.patch").write_text("".join(partial_parts), encoding="utf-8")

print("分片 A 完成：注入补丁 4 个、fix.patch、partial.patch")
