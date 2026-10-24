import datetime
import sqlalchemy
from sqlalchemy import Table, Column, String, DateTime, MetaData, event, select, delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from typing import Any, Dict


def _enable_write_ahead_log(dbapi_connection: Any, connection_record: Any):
    """ In WAL mode readers don't block the writer (and vice versa), so concurrent searches don't lock each other out """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


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
        if insert_time:
            # double interval on each retry
            interval = 2 * (datetime.datetime.now() - insert_time)
        else:
            interval = datetime.timedelta(days=7)
        return datetime.datetime.now() + interval

    def cache_match_failure(self, track_id: str):
        """ notifies that matching failed for the given track_id """
        # Either update the next_retry time if track_id already exists, otherwise create a new entry.
        # A single upsert statement, so concurrent callers can't both insert the same track_id
        next_retry = self._get_next_retry_time()
        statement = sqlite_insert(self.match_failures).values(
            track_id=track_id, insert_time=datetime.datetime.now(), next_retry=next_retry
        ).on_conflict_do_update(index_elements=[self.match_failures.c.track_id], set_={"next_retry": next_retry})
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


class TrackMatchCache:
    """
    Non-persistent mapping of spotify ids -> tidal_ids
    This should NOT be accessed concurrently from multiple processes
    """
    data: Dict[str, int] = {}

    def get(self, track_id: str) -> int | None:
        return self.data.get(track_id, None)

    def insert(self, mapping: tuple[str, int]):
        self.data[mapping[0]] = mapping[1]


# Main singleton instance
failure_cache = MatchFailureDatabase()
track_match_cache = TrackMatchCache()
