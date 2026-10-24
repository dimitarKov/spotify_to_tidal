# tests/unit/test_sync.py
# pyright: reportPrivateUsage=false

import asyncio
import pytest
import requests
import spotipy
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Sequence
from unittest import mock
from pytest_mock import MockerFixture
from tidalapi.exceptions import TooManyRequests

from spotify_to_tidal import sync as _sync
from spotify_to_tidal.sync import _extract_track, _is_transient_error, get_tracks_from_spotify_playlist, repeat_on_request_error
from spotify_to_tidal.type import SpotifyPlaylist, SpotifyTrack, SyncConfig


def make_track(track_id: str, name: str = "Song", type_: str = "track") -> SpotifyTrack:
    return {
        "id": track_id,
        "name": name,
        "type": type_,
        "album": {"name": "Album", "artists": [{"name": "Artist"}]},
        "artists": [{"name": "Artist"}],
        "track_number": 1,
        "duration_ms": 180000,
        "external_ids": {"isrc": "ISRC" + track_id},
    }


def make_page(entries: List[Dict[str, Any]], offset: int = 0, total: int | None = None, limit: int = 2) -> Dict[str, Any]:
    total = len(entries) if total is None else total
    return {
        "items": entries,
        "limit": limit,
        "total": total,
        "next": "next-url" if offset + limit < total else None,
    }


def make_playlist(playlist_id: str, name: str) -> SpotifyPlaylist:
    return {"id": playlist_id, "name": name, "description": None, "owner": {"id": "me"}}


# Test _extract_track
def test_extract_track_from_item_field():
    track = make_track("a")
    assert _extract_track({"item": track, "track": True}) == track


def test_extract_track_from_legacy_track_field():
    # saved tracks (/me/tracks) and older playlist responses use 'track'
    track = make_track("a")
    assert _extract_track({"track": track}) == track


def test_extract_track_ignores_boolean_and_missing():
    assert _extract_track({"track": True}) is None
    assert _extract_track({"item": None, "track": False}) is None
    assert _extract_track({}) is None


# Test get_tracks_from_spotify_playlist
def test_get_tracks_from_spotify_playlist_new_response_shape():
    pages = {
        0: make_page([{"item": make_track("a"), "track": True},
                      {"item": make_track("b"), "track": True}], offset=0, total=3),
        2: make_page([{"item": make_track("c"), "track": True}], offset=2, total=3),
    }

    def playlist_items(playlist_id: str, fields: str, offset: int, additional_types: Sequence[str]) -> Dict[str, Any]:
        return pages[offset]

    session = mock.Mock()
    session.playlist_items.side_effect = playlist_items

    tracks = asyncio.run(get_tracks_from_spotify_playlist(session, make_playlist("pl", "Playlist")))

    assert [t["id"] for t in tracks] == ["a", "b", "c"]
    # the fields filter must ask for the renamed 'item' field
    assert "items(item(" in session.playlist_items.call_args.kwargs["fields"]


def test_get_tracks_from_spotify_playlist_filters_episodes_and_empty_entries():
    page = make_page([
        {"item": make_track("a"), "track": True},
        {"item": make_track("ep", type_="episode"), "track": False},
        {"item": None, "track": False},
        {"track": make_track("legacy")},
    ], limit=50)
    session = mock.Mock()
    session.playlist_items.return_value = page

    tracks = asyncio.run(get_tracks_from_spotify_playlist(session, make_playlist("pl", "Playlist")))

    assert [t["id"] for t in tracks] == ["a", "legacy"]


# Test repeat_on_request_error
def spotify_error(status: int, reason: str | None = None, headers: Mapping[str, str] | None = None) -> spotipy.exceptions.SpotifyException:
    return spotipy.exceptions.SpotifyException(status, -1, "error", reason=reason, headers=headers)


def http_error(status: int, headers: Mapping[str, str] | None = None) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.exceptions.HTTPError(response=response)


@pytest.fixture
def sleeps(mocker: MockerFixture) -> List[float]:
    """ Replaces asyncio.sleep in sync.py and records requested durations """
    recorded: List[float] = []

    async def fake_sleep(seconds: float):
        recorded.append(seconds)

    mocker.patch.object(_sync.asyncio, "sleep", fake_sleep)
    return recorded


def failing_then(results: Sequence[Exception | str]) -> tuple[Callable[[], Awaitable[str]], List[None]]:
    """ Async function that raises/returns each item of results in turn """
    calls: List[None] = []

    async def function() -> str:
        calls.append(None)
        result = results[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result

    return function, calls


@pytest.mark.parametrize("error, expected", [
    (spotify_error(429), True),
    (spotify_error(503), True),
    (spotify_error(429, reason="QUOTA_EXCEEDED"), False),
    (spotify_error(403), False),
    (spotify_error(404), False),
    (http_error(429), True),
    (http_error(502), True),
    (http_error(412), False),
    (http_error(404), False),
    (requests.exceptions.ConnectionError(), True),
    (requests.exceptions.Timeout(), True),
    (TooManyRequests(), True),
])
def test_is_transient_error(error: Exception, expected: bool):
    assert _is_transient_error(error) is expected


def test_non_transient_error_is_raised_without_retrying(sleeps: List[float]):
    function, calls = failing_then([spotify_error(403)])
    with pytest.raises(spotipy.exceptions.SpotifyException):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 1
    assert sleeps == []


def test_quota_exceeded_is_not_retried(sleeps: List[float]):
    function, calls = failing_then([spotify_error(429, reason="QUOTA_EXCEEDED")])
    with pytest.raises(spotipy.exceptions.SpotifyException):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 1


def test_transient_error_is_retried_until_success(sleeps: List[float]):
    function, calls = failing_then([requests.exceptions.ConnectionError(), spotify_error(503), "ok"])
    assert asyncio.run(repeat_on_request_error(function)) == "ok"
    assert len(calls) == 3
    assert sleeps == [1, 10]  # default back-off schedule


def test_retry_after_header_is_respected(sleeps: List[float]):
    function, _ = failing_then([spotify_error(429, headers={"Retry-After": "7"}), "ok"])
    assert asyncio.run(repeat_on_request_error(function)) == "ok"
    assert sleeps == [7]


def test_tidal_retry_after_is_respected(sleeps: List[float]):
    function, _ = failing_then([TooManyRequests(retry_after=3), "ok"])
    assert asyncio.run(repeat_on_request_error(function)) == "ok"
    assert sleeps == [3]


def test_gives_up_after_retries_are_exhausted(sleeps: List[float]):
    function, calls = failing_then([spotify_error(500)] * 6)
    with pytest.raises(SystemExit):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 6


def test_gives_up_when_asked_to_wait_too_long(sleeps: List[float]):
    function, calls = failing_then([spotify_error(429, headers={"Retry-After": "86400"}), "ok"])
    with pytest.raises(SystemExit):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 1
    assert sleeps == []


# Test sync_playlists_wrapper
EMPTY_CONFIG: SyncConfig = {"spotify": {"client_id": "", "client_secret": "", "username": "", "redirect_uri": ""}}


def test_inaccessible_playlist_is_skipped(mocker: MockerFixture):
    synced: List[str] = []

    async def fake_sync_playlist(spotify_session: Any, tidal_session: Any, spotify_playlist: SpotifyPlaylist, tidal_playlist: Any, config: Any):
        if spotify_playlist["id"] == "not-mine":
            raise spotify_error(403)
        synced.append(spotify_playlist["id"])

    mocker.patch.object(_sync, "sync_playlist", fake_sync_playlist)
    playlists: List[_sync.PlaylistMapping] = [(make_playlist("not-mine", "Not mine"), None), (make_playlist("mine", "Mine"), None)]
    _sync.sync_playlists_wrapper(mock.Mock(), mock.Mock(), playlists, EMPTY_CONFIG)
    assert synced == ["mine"]


def test_other_spotify_errors_still_abort(mocker: MockerFixture):
    async def fake_sync_playlist(*args: Any):
        raise spotify_error(400)

    mocker.patch.object(_sync, "sync_playlist", fake_sync_playlist)
    with pytest.raises(spotipy.exceptions.SpotifyException):
        _sync.sync_playlists_wrapper(mock.Mock(), mock.Mock(), [(make_playlist("x", "X"), None)], EMPTY_CONFIG)
