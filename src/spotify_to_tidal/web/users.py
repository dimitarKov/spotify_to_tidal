import re
import spotipy
from pathlib import Path

from ..context import SyncContext
from ..storage import FileTidalSessionStore
from ..type import SyncConfig

_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class UserData:
    """ Where one user's state is kept: a directory per user under the data directory.
        (Local files for now; a hosted deployment can swap in database-backed stores) """

    def __init__(self, data_dir: Path, user_id: str):
        # user ids become directory names, so only allow safe characters (Spotify user ids are alphanumeric)
        if not _USER_ID.match(user_id):
            raise ValueError(f"Invalid user id: {user_id!r}")
        self.user_id = user_id
        self.directory = data_dir / user_id

    def _ensure_directory(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        return self.directory

    def sync_context(self, config: SyncConfig) -> SyncContext:
        return SyncContext.local_files(config, self._ensure_directory())

    def tidal_session_store(self) -> FileTidalSessionStore:
        return FileTidalSessionStore(self._ensure_directory() / ".session.yml")

    def spotify_token_cache(self) -> spotipy.CacheFileHandler:
        return spotipy.CacheFileHandler(cache_path=str(self._ensure_directory() / ".spotify_token"))
