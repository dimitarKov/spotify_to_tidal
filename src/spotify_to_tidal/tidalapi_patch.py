# This module works around gaps in tidalapi and deliberately uses its private members (_etag, _base_url, _reparse)
# pyright: reportPrivateUsage=false

import asyncio
import math
import requests
import time
from typing import Any, Callable, Dict, List, Mapping, Sequence, TypeVar
from tidalapi.media import Track
from tidalapi.playlist import Playlist, UserPlaylist
from tidalapi.session import Session
from tidalapi.user import Favorites, User
from .progress import gather_with_progress
from .reporting import get_reporter

T = TypeVar('T')

def _retry_on_stale_etag(playlist: UserPlaylist, write: Callable[[], T], attempts: int = 5, base_delay: float = 1) -> T:
    """ Tidal rejects playlist writes with 412 Precondition Failed when the ETag we send is stale, which happens
        during rapid chunked edits as its replicas lag behind. The write was not applied, so refresh the ETag,
        back off and try again. Any other error is raised immediately """
    for attempt in range(attempts):
        try:
            return write()
        except requests.HTTPError as e:
            if e.response is None or e.response.status_code != 412 or attempt == attempts - 1:
                raise
            time.sleep(base_delay * 2**attempt)
            playlist._reparse()
    raise AssertionError("unreachable")

def _remove_indices_from_playlist(playlist: UserPlaylist, indices: Sequence[int]):
    def _delete():
        # build the headers on each attempt so a retry sends the refreshed ETag
        headers: Dict[str, str] = {}
        if playlist._etag is not None:
            headers['If-None-Match'] = playlist._etag
        index_string = ",".join(map(str, indices))
        playlist.request.request('DELETE', (playlist._base_url + '/items/%s') % (playlist.id, index_string), headers=headers)
    _retry_on_stale_etag(playlist, _delete)
    playlist._reparse()

def clear_tidal_playlist(playlist: UserPlaylist, chunk_size: int=20):
    with get_reporter().progress("Erasing existing tracks from Tidal playlist", total=playlist.num_tracks) as progress:
        while playlist.num_tracks:
            indices = range(min(playlist.num_tracks, chunk_size))
            _remove_indices_from_playlist(playlist, indices)
            progress.update(len(indices))

def add_multiple_tracks_to_playlist(playlist: UserPlaylist, track_ids: Sequence[int], chunk_size: int=20):
    offset = 0
    with get_reporter().progress("Adding new tracks to Tidal playlist", total=len(track_ids)) as progress:
        while offset < len(track_ids):
            count = min(chunk_size, len(track_ids) - offset)
            chunk = [str(track_id) for track_id in track_ids[offset:offset+chunk_size]]
            _retry_on_stale_etag(playlist, lambda: playlist.add(chunk))
            offset += count
            progress.update(count)

async def _get_all_chunks(url: str, session: Session, parser: Callable[[Any], T], params: Mapping[str, Any] | None = None) -> List[T]:
    """
        Helper function to get all items from a Tidal endpoint in parallel
        The main library doesn't provide the total number of items or expose the raw json, so use this wrapper instead
    """
    def _make_request(offset: int=0) -> Dict[str, Any]:
        # requests run concurrently in threads, so each needs its own params rather than mutating a shared dict
        new_params = {**(params or {}), 'offset': offset}
        return session.request.map_request(url, params=new_params)

    def _get_chunk(offset: int) -> List[T]:
        return session.request.map_json(_make_request(offset), parse=parser)

    first_chunk_raw = _make_request()
    limit: int = first_chunk_raw['limit']
    total: int = first_chunk_raw['totalNumberOfItems']
    items: List[T] = session.request.map_json(first_chunk_raw, parse=parser)

    if len(items) < total:
        offsets = [limit * n for n in range(1, math.ceil(total/limit))]
        extra_results = await gather_with_progress(
            [asyncio.to_thread(_get_chunk, offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            items.extend(extra_result)
    return items

async def get_all_favorites(favorites: Favorites, order: str = "NAME", order_direction: str = "ASC", chunk_size: int=100) -> List[Track]:
    """ Get all favorites from Tidal playlist in chunks """
    params: Dict[str, Any] = {
        "limit": chunk_size,
        "order": order,
        "orderDirection": order_direction,
    }
    return await _get_all_chunks(f"{favorites.base_url}/tracks", session=favorites.session, parser=favorites.session.parse_track, params=params)

async def get_all_playlists(user: User, chunk_size: int=10) -> List[Playlist]:
    """ Get all user playlists from Tidal in chunks """
    get_reporter().info("Loading playlists from Tidal user")
    params: Dict[str, Any] = {
        "limit": chunk_size,
    }
    return await _get_all_chunks(f"users/{user.id}/playlists", session=user.session, parser=user.playlist.parse_factory, params=params)

async def get_all_playlist_tracks(playlist: Playlist, chunk_size: int=20) -> List[Track]:
    """ Get all tracks from Tidal playlist in chunks """
    params: Dict[str, Any] = {
        "limit": chunk_size,
    }
    get_reporter().info(f"Loading tracks from Tidal playlist '{playlist.name}'")
    return await _get_all_chunks(f"{playlist._base_url%playlist.id}/tracks", session=playlist.session, parser=playlist.session.parse_track, params=params)
