""" Shapes of the Spotify Web API objects used by spotify_to_tidal.
    Only the fields we request/read are declared; see the `fields` filters in sync.py """

from typing import Any, Dict, List, Optional, TypedDict
from typing_extensions import NotRequired


class SpotifyArtist(TypedDict):
    name: Optional[str]


class SpotifyAlbum(TypedDict):
    name: str
    artists: List[SpotifyArtist]


class SpotifyTrack(TypedDict):
    id: Optional[str]  # None for local files
    name: str
    album: SpotifyAlbum
    artists: List[SpotifyArtist]
    track_number: int
    duration_ms: int
    external_ids: NotRequired[Dict[str, str]]
    type: NotRequired[str]  # 'track' or 'episode'


class SpotifyUser(TypedDict):
    id: str


class SpotifyPlaylist(TypedDict):
    id: str
    name: str
    description: Optional[str]
    owner: SpotifyUser


class SpotifyPage(TypedDict):
    """ A page of results from a paginated endpoint """
    items: List[Any]
    limit: int
    total: int
    next: Optional[str]
