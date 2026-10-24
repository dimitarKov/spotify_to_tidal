from dataclasses import dataclass, field
from pathlib import Path

from .cache import InMemoryMatchFailureStore, MatchFailureDatabase, TrackMatchCache
from .storage import FileNotFoundReport, InMemoryNotFoundReport, MatchFailureStore, NotFoundReport
from .type import SyncConfig


@dataclass
class SyncContext:
    """ Everything one user's sync needs besides the Spotify and Tidal sessions.
        Defaults keep everything in memory; use local_files() for the CLI's files in a directory """
    config: SyncConfig
    failure_cache: MatchFailureStore = field(default_factory=InMemoryMatchFailureStore)
    not_found: NotFoundReport = field(default_factory=InMemoryNotFoundReport)
    track_match_cache: TrackMatchCache = field(default_factory=TrackMatchCache)

    @classmethod
    def local_files(cls, config: SyncConfig, directory: str | Path = ".") -> "SyncContext":
        """ State in files: .cache.db and "songs not found.txt" in `directory` """
        directory = Path(directory)
        return cls(config,
                   failure_cache=MatchFailureDatabase(str(directory / ".cache.db")),
                   not_found=FileNotFoundReport(directory / "songs not found.txt"))
