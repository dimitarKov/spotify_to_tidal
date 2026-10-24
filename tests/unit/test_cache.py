# tests/unit/test_cache.py

import datetime
import pytest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from pytest_mock import MockerFixture
from sqlalchemy import Engine, create_engine, select, update
from spotify_to_tidal.cache import MatchFailureDatabase, TrackMatchCache


# Setup an in-memory SQLite database for testing
@pytest.fixture
def in_memory_db() -> Engine:
    engine = create_engine("sqlite:///:memory:")
    return engine


# Test MatchFailureDatabase
def test_cache_match_failure(in_memory_db: Engine, mocker: MockerFixture):
    mocker.patch(
        "spotify_to_tidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    failure_db = MatchFailureDatabase()

    track_id = "test_track"
    failure_db.cache_match_failure(track_id)

    with failure_db.engine.connect() as connection:
        result = connection.execute(
            select(failure_db.match_failures).where(
                failure_db.match_failures.c.track_id == track_id
            )
        ).fetchone()
        assert result is not None
        assert result.track_id == track_id


def test_has_match_failure(in_memory_db: Engine, mocker: MockerFixture):
    mocker.patch(
        "spotify_to_tidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    failure_db = MatchFailureDatabase()

    track_id = "test_track"
    failure_db.cache_match_failure(track_id)

    assert failure_db.has_match_failure(track_id) is True


def test_remove_match_failure(in_memory_db: Engine, mocker: MockerFixture):
    mocker.patch(
        "spotify_to_tidal.cache.sqlalchemy.create_engine", return_value=in_memory_db
    )
    failure_db = MatchFailureDatabase()

    track_id = "test_track"
    failure_db.cache_match_failure(track_id)
    failure_db.remove_match_failure(track_id)

    with failure_db.engine.connect() as connection:
        result = connection.execute(
            select(failure_db.match_failures).where(
                failure_db.match_failures.c.track_id == track_id
            )
        ).fetchone()
        assert result is None


# Test TrackMatchCache
def test_track_match_cache_insert():
    track_cache = TrackMatchCache()
    track_cache.insert(("spotify_id", 123))
    assert track_cache.get("spotify_id") == 123


def test_track_match_cache_get():
    track_cache = TrackMatchCache()
    track_cache.insert(("spotify_id", 123))
    assert track_cache.get("spotify_id") == 123
    assert track_cache.get("nonexistent_id") is None

# Test concurrent use (tidal_search runs up to max_concurrency searches in threads)


def test_concurrent_writes_from_many_threads(tmp_path: Path):
    failure_db = MatchFailureDatabase(str(tmp_path / "cache.db"))

    def work(worker: int):
        for i in range(30):
            track_id = f"track-{i % 10}"  # threads contend for the same rows
            failure_db.cache_match_failure(track_id)
            failure_db.has_match_failure(track_id)
            if i % 3 == worker % 3:
                failure_db.remove_match_failure(track_id)

    with ThreadPoolExecutor(max_workers=16) as pool:
        for future in [pool.submit(work, worker) for worker in range(16)]:
            future.result()  # re-raises e.g. "database is locked"


def test_cache_match_failure_again_keeps_insert_time(tmp_path: Path):
    failure_db = MatchFailureDatabase(str(tmp_path / "cache.db"))
    failure_db.cache_match_failure("track")
    query = select(failure_db.match_failures).where(failure_db.match_failures.c.track_id == "track")
    with failure_db.engine.connect() as connection:
        first = connection.execute(query).one()
    failure_db.cache_match_failure("track")
    with failure_db.engine.connect() as connection:
        rows = connection.execute(query).all()
    assert len(rows) == 1
    assert rows[0].insert_time == first.insert_time
    assert rows[0].next_retry >= first.next_retry


def test_write_ahead_log_enabled(tmp_path: Path):
    failure_db = MatchFailureDatabase(str(tmp_path / "cache.db"))
    with failure_db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


# Test retry back-off


def get_failure_times(failure_db: MatchFailureDatabase, track_id: str) -> tuple[datetime.datetime, datetime.datetime]:
    with failure_db.engine.connect() as connection:
        row = connection.execute(select(failure_db.match_failures).where(failure_db.match_failures.c.track_id == track_id)).one()
    return row.insert_time, row.next_retry


def first_failed_days_ago(failure_db: MatchFailureDatabase, track_id: str, days: float):
    with failure_db.engine.connect() as connection:
        with connection.begin():
            connection.execute(update(failure_db.match_failures)
                               .where(failure_db.match_failures.c.track_id == track_id)
                               .values(insert_time=datetime.datetime.now() - datetime.timedelta(days=days)))


def days_from_now(time: datetime.datetime) -> float:
    return (time - datetime.datetime.now()) / datetime.timedelta(days=1)


def test_first_failure_waits_a_week(tmp_path: Path):
    failure_db = MatchFailureDatabase(str(tmp_path / "cache.db"))
    failure_db.cache_match_failure("track")
    _, next_retry = get_failure_times(failure_db, "track")
    assert abs(days_from_now(next_retry) - 7) < 0.01


def test_quick_repeated_failure_still_waits_a_week(tmp_path: Path):
    failure_db = MatchFailureDatabase(str(tmp_path / "cache.db"))
    failure_db.cache_match_failure("track")
    failure_db.cache_match_failure("track")
    _, next_retry = get_failure_times(failure_db, "track")
    assert abs(days_from_now(next_retry) - 7) < 0.01


def test_repeated_failures_double_the_wait(tmp_path: Path):
    failure_db = MatchFailureDatabase(str(tmp_path / "cache.db"))
    failure_db.cache_match_failure("track")

    # retried after the first week and failed again: wait twice the time since the first failure
    first_failed_days_ago(failure_db, "track", 7)
    failure_db.cache_match_failure("track")
    _, next_retry = get_failure_times(failure_db, "track")
    assert abs(days_from_now(next_retry) - 14) < 0.01

    # failed again 21 days after the first failure: wait 42 days
    first_failed_days_ago(failure_db, "track", 21)
    failure_db.cache_match_failure("track")
    _, next_retry = get_failure_times(failure_db, "track")
    assert abs(days_from_now(next_retry) - 42) < 0.01
