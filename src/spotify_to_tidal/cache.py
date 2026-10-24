import datetime
import sqlalchemy
from sqlalchemy import Table, Column, String, DateTime, MetaData, event, select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
import threading
from typing import Any, Dict, Tuple


def _enable_write_ahead_log(dbapi_connection: Any, connection_record: Any):
    """ In WAL mode readers don't block the writer (and vice versa), so concurrent searches don't lock each other out """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def next_retry_time(insert_time: datetime.datetime | None = None) -> datetime.datetime:
    """ When to search again for a track that failed to match, given when it first failed (None: it just did) """
    first_interval = datetime.timedelta(days=7)
    if insert_time:
        # double interval on each retry. Never less than the first interval, e.g. if the same track
        # failed twice in quick succession (a playlist containing it twice)
        interval = max(first_interval, 2 * (datetime.datetime.now() - insert_time))
    else:
        interval = first_interval
    return datetime.datetime.now() + interval


class MatchFailureDatabase:
    """ 
    sqlite database of match failures which persists between runs
    this can be used concurrently between multiple processes
    """

    def __init__(self, filename: str = '.cache.db'):
        # wait up to 30s for another connection's write lock instead of failing with "database is locked"
        self.engine = sqlalchemy.create_engine(f"sqlite:///{filename}", connect_args={"timeout": 30})
        event.listen(self.engine, "connect", _enable_write_ahead_log)
        meta = MetaData()
        self.match_failures = Table('match_failures', meta,
                                    Column('track_id', String,
                                           primary_key=True),
                                    Column('insert_time', DateTime),
                                    Column('next_retry', DateTime),
                                    sqlite_autoincrement=False)
        meta.create_all(self.engine)

    def _get_next_retry_time(self, insert_time: datetime.datetime | None = None) -> datetime.datetime:
        return next_retry_time(insert_time)

    def cache_match_failure(self, track_id: str):
        """ notifies that matching failed for the given track_id """
        # Either update the next_retry time if track_id already exists, otherwise create a new entry.
        # The write is a single upsert statement, so concurrent callers can't both insert the same track_id.
        # The existing row is read beforehand, outside the write transaction, to avoid read-then-write deadlocks
        insert_time_statement = select(self.match_failures.c.insert_time).where(
            self.match_failures.c.track_id == track_id)
        with self.engine.connect() as connection:
            insert_time: datetime.datetime | None = connection.execute(insert_time_statement).scalar()
        statement = sqlite_insert(self.match_failures).values(
            track_id=track_id, insert_time=datetime.datetime.now(), next_retry=self._get_next_retry_time()
        ).on_conflict_do_update(index_elements=[self.match_failures.c.track_id],
                                set_={"next_retry": self._get_next_retry_time(insert_time)})
        with self.engine.connect() as connection:
            with connection.begin():
                connection.execute(statement)

    def has_match_failure(self, track_id: str) -> bool:
        """ checks if there was a recent search for which matching failed with the given track_id """
        statement = select(self.match_failures.c.next_retry).where(
            self.match_failures.c.track_id == track_id)
        with self.engine.connect() as connection:
            match_failure = connection.execute(statement).fetchone()
            if match_failure:
                return match_failure.next_retry > datetime.datetime.now()
            return False

    def remove_match_failure(self, track_id: str):
        """ removes match failure from the database """
        statement = delete(self.match_failures).where(
            self.match_failures.c.track_id == track_id)
        with self.engine.connect() as connection:
            with connection.begin():
                connection.execute(statement)


class InMemoryMatchFailureStore:
    """ Match failures kept in memory, with the same retry back-off as MatchFailureDatabase. Thread-safe """

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: Dict[str, Tuple[datetime.datetime, datetime.datetime]] = {}  # track_id -> (insert_time, next_retry)

    def cache_match_failure(self, track_id: str):
        with self._lock:
            existing = self._failures.get(track_id)
            if existing:
                self._failures[track_id] = (existing[0], next_retry_time(existing[0]))
            else:
                self._failures[track_id] = (datetime.datetime.now(), next_retry_time())

    def has_match_failure(self, track_id: str) -> bool:
        with self._lock:
            failure = self._failures.get(track_id)
        return failure is not None and failure[1] > datetime.datetime.now()

    def remove_match_failure(self, track_id: str):
        with self._lock:
            self._failures.pop(track_id, None)


class TrackMatchCache:
    """
    Non-persistent mapping of spotify ids -> tidal_ids for one sync run
    This should NOT be accessed concurrently from multiple processes
    """

    def __init__(self):
        self.data: Dict[str, int] = {}

    def get(self, track_id: str) -> int | None:
        return self.data.get(track_id, None)

    def insert(self, mapping: tuple[str, int]):
        self.data[mapping[0]] = mapping[1]

