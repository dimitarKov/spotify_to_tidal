# Minimal type stubs for the parts of spotipy used by spotify_to_tidal (spotipy ships no type information).
# Web API responses are untyped JSON, so they are returned as Any and given TypedDict types by the caller.

from typing import Any, Iterable

from . import exceptions as exceptions
from .cache_handler import (
    CacheFileHandler as CacheFileHandler,
    CacheHandler as CacheHandler,
    MemoryCacheHandler as MemoryCacheHandler,
)
from .exceptions import (
    SpotifyBaseException as SpotifyBaseException,
    SpotifyException as SpotifyException,
    SpotifyOauthError as SpotifyOauthError,
)

class SpotifyOAuth:
    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        state: str | None = None,
        scope: str | Iterable[str] | None = None,
        cache_path: str | None = None,
        username: str | None = None,
        proxies: dict[str, str] | None = None,
        show_dialog: bool = False,
        requests_session: Any = True,
        requests_timeout: float | None = None,
        open_browser: bool = True,
        cache_handler: CacheHandler | None = None,
    ) -> None: ...
    def get_access_token(self, code: str | None = None, as_dict: bool = True, check_cache: bool = True) -> Any: ...

class Spotify:
    def __init__(
        self,
        auth: str | None = None,
        requests_session: Any = True,
        client_credentials_manager: Any = None,
        oauth_manager: SpotifyOAuth | None = None,
        auth_manager: Any = None,
        proxies: dict[str, str] | None = None,
        requests_timeout: float = 5,
        status_forcelist: Iterable[int] | None = None,
        retries: int = 3,
        status_retries: int = 3,
        backoff_factor: float = 0.3,
        language: str | None = None,
    ) -> None: ...
    def current_user(self) -> Any: ...
    def current_user_playlists(self, limit: int = 50, offset: int = 0) -> Any: ...
    def current_user_saved_tracks(self, limit: int = 20, offset: int = 0, market: str | None = None) -> Any: ...
    def playlist(
        self,
        playlist_id: str,
        fields: str | None = None,
        market: str | None = None,
        additional_types: Iterable[str] = ("track",),
    ) -> Any: ...
    def playlist_items(
        self,
        playlist_id: str,
        fields: str | None = None,
        limit: int = 50,
        offset: int = 0,
        market: str | None = None,
        additional_types: Iterable[str] = ("track", "episode"),
    ) -> Any: ...
