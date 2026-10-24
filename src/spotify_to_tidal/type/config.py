from typing import List, TypedDict
from typing_extensions import NotRequired


class SpotifyConfig(TypedDict):
    client_id: str
    client_secret: str
    username: str
    redirect_uri: str
    open_browser: NotRequired[bool]


class PlaylistConfig(TypedDict):
    spotify_id: str
    tidal_id: str


class SyncConfig(TypedDict):
    """ Contents of config.yml; see example_config.yml """
    spotify: SpotifyConfig
    sync_playlists: NotRequired[List[PlaylistConfig]]
    excluded_playlists: NotRequired[List[str]]
    sync_favorites_default: NotRequired[bool]
    max_concurrency: NotRequired[int]
    rate_limit: NotRequired[float]
