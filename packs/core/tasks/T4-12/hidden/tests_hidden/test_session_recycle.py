"""T4-12 隐藏测试 · 会话回收窗口。

口径：一条对话"是否正在使用"在自动清理与显式删除两个出口必须共用同一个判定，
且该判定必须覆盖"已被认领、还没拿到执行权"的窗口；时间戳不可读只参与最旧优先
的条数回收，绝不触发保留期淘汰。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from astock_backtester.ai.errors import AiSessionBusy
from astock_backtester.ai.facade import AiService
from astock_backtester.ai.sessions import SESSION_SCHEMA_VERSION, SessionStore, sanitize_session_id


def _iso_days_ago(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _write_session(store: SessionStore, session_id: str, *, updated_at: str | None, title: str = "会话") -> str:
    """直接落盘一条会话（绕过 save 的自动清理），让回收语义成为被测目标。"""
    store.directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "schema_version": SESSION_SCHEMA_VERSION,
        "title": title,
        "created_at": updated_at,
        "messages": [],
        "display": [{"role": "user", "content": title}],
    }
    if updated_at is not None:
        payload["updated_at"] = updated_at
    (store.directory / f"{session_id}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return session_id


def _service(tmp_path) -> AiService:
    return AiService(
        cache_dir=str(tmp_path / "本地数据仓"),
        backend=SimpleNamespace(),
        log=lambda *args, **kwargs: None,
    )


def _release(service: AiService, entry) -> None:
    with service._session_locks_guard:
        entry.refs = max(0, entry.refs - 1)


def test_prune_spares_a_session_whose_turn_is_claimed_but_not_started(tmp_path):
    """认领窗口（引用 > 0、锁仍空闲）内的会话不得被自动清理。

    清理由每次 save 触发；worker 尚未持锁时若被判成空闲，正在跑的对话会被
    整条回收、再被 finally 写回来——列表闪一下又消失，失败的那一轮直接丢。
    """
    service = _service(tmp_path)
    session = service._sessions.create("即将开跑的会话")
    safe = sanitize_session_id(session["session_id"])
    assert safe is not None
    path = service._sessions.directory / f"{safe}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = _iso_days_ago(90)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    entry = service._session_lock(safe)  # 引用 > 0，锁仍空闲：认领窗口
    try:
        assert entry.lock.locked() is False
        removed = service._sessions.prune(max_sessions=0, retention_days=30)
        assert removed == [], "认领窗口内的会话不得被保留期回收"
        assert service._sessions.get(safe) is not None
    finally:
        _release(service, entry)


def test_delete_refuses_in_the_same_claimed_window_prune_spares(tmp_path):
    """删除与自动清理必须服从同一个忙判定：认领窗口内删除必须被拒绝。"""
    service = _service(tmp_path)
    session = service._sessions.create("认领中的会话")
    safe = sanitize_session_id(session["session_id"])
    assert safe is not None

    entry = service._session_lock(safe)
    try:
        with pytest.raises(AiSessionBusy):
            service.delete_session(session["session_id"])
        assert service._sessions.get(safe) is not None
    finally:
        _release(service, entry)
    # 窗口结束（引用归还、锁未持有）后即可正常删除
    assert service.delete_session(session["session_id"]) is True


def test_unreadable_timestamp_survives_the_retention_sweep(tmp_path):
    """读不出时间戳的会话不适用保留期淘汰；条数回收需要时它按最旧一档淘汰。"""
    store = SessionStore(tmp_path)
    broken = _write_session(store, "session-broken", updated_at=None)
    fresh = _write_session(store, "session-fresh", updated_at=_iso_days_ago(1))

    removed = store.prune(max_sessions=0, retention_days=30)
    assert removed == [], "读不出时间戳不是销毁文件的理由"
    assert store.get(broken) is not None
    assert store.get(fresh) is not None

    # 条数上限需要回收时，坏时间戳的排在最旧一档被淘汰，可读的保留
    removed = store.prune(max_sessions=1, retention_days=0)
    assert removed == [broken]
    assert store.get(fresh) is not None
