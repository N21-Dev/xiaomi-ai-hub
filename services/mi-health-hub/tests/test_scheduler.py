"""调度器测试。

重点是**跨线程使用 SQLite 连接**这个坑：
APScheduler 在线程池里执行任务，如果连接是在主线程（或 __init__ 里）建好的，
任务一跑就抛 `ProgrammingError: SQLite objects created in a thread can only be used
in that same thread`。这个错误实际发生过——哨兵任务每次都在静默失败。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from mi_health_hub.config import load_config
from mi_health_hub.scheduler import Scheduler, _parse_hhmm
from mi_health_hub.store.repository import Repository, connect, init_db


def make_scheduler(tmp_path) -> Scheduler:
    config = load_config(tmp_path / "none.yaml")
    config.paths.db = tmp_path / "sched.db"
    config.paths.raw = tmp_path / "raw"
    config.paths.output = tmp_path / "out"
    config.paths.backup = tmp_path / "bak"
    config.paths.ensure()
    return Scheduler(config)


def test_connection_created_in_main_thread_fails_in_worker_thread(tmp_path):
    """复现原生 sqlite3 的限制，说明为什么必须每线程一条连接。"""
    conn = connect(tmp_path / "x.db")
    init_db(conn)
    repo = Repository(conn)
    error: list[Exception] = []

    def work() -> None:
        try:
            repo.mark_sync_success("steps", 1)
        except Exception as exc:  # noqa: BLE001
            error.append(exc)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    conn.close()

    assert error, "预期跨线程使用会报错，但没有"
    assert isinstance(error[0], sqlite3.ProgrammingError)


def test_scheduler_open_repo_works_inside_worker_thread(tmp_path):
    """修复后的用法：在线程内开连接，可以在该线程正常读写。"""
    scheduler = make_scheduler(tmp_path)
    observed: dict[str, object] = {}

    def work() -> None:
        conn, repo = scheduler.open_repo()
        try:
            repo.mark_sync_success("steps", 12345)
            observed["cursor"] = repo.get_sync_state("steps")["cursor_ts"]
            observed["integrity"] = repo.integrity_check()
        finally:
            conn.close()

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()

    assert observed["cursor"] == 12345
    assert observed["integrity"] == "ok"

    # 主线程另开一条也能读到刚写下的值（说明落盘成功，不是线程局部缓存）
    conn, repo = scheduler.open_repo()
    try:
        assert repo.get_sync_state("steps")["cursor_ts"] == 12345
    finally:
        conn.close()


def test_run_locked_executes_job_and_releases_lock(tmp_path):
    scheduler = make_scheduler(tmp_path)
    calls: list[str] = []

    def job(repo: Repository) -> None:
        calls.append(repo.integrity_check())

    scheduler._run_locked("测试", job)
    scheduler._run_locked("测试", job)  # 能跑第二次说明锁已释放

    assert calls == ["ok", "ok"]


def test_run_locked_swallows_job_exception_and_releases_lock(tmp_path):
    scheduler = make_scheduler(tmp_path)
    attempts: list[int] = []

    def boom(repo: Repository) -> None:  # noqa: ARG001
        attempts.append(1)
        raise RuntimeError("故意失败")

    scheduler._run_locked("会失败的任务", boom)
    # 失败不能让锁泄漏，也不该把异常抛给调度线程
    scheduler._run_locked("后续任务", lambda repo: attempts.append(2))
    assert attempts == [1, 2]


def test_run_locked_skips_when_already_running(tmp_path):
    """任务重叠时要跳过而不是排队堆积（否则接口故障时会打出请求风暴）。"""
    scheduler = make_scheduler(tmp_path)
    import mi_health_hub.scheduler as mod

    executed: list[str] = []

    def job(repo: Repository) -> None:  # noqa: ARG001
        executed.append("ran")

    mod._sync_lock.acquire()
    try:
        scheduler._run_locked("重叠任务", job)
    finally:
        mod._sync_lock.release()

    assert executed == []  # 被跳过


@pytest.mark.parametrize(
    "value,expected",
    [("04:30", (4, 30)), ("00:00", (0, 0)), ("23:59", (23, 59)), ("bad", (4, 30))],
)
def test_parse_hhmm(value, expected):
    assert _parse_hhmm(value) == expected
