# tests/unit/test_sync.py

import asyncio
from unittest import mock
from spotify_to_tidal.sync import _extract_track, get_tracks_from_spotify_playlist


def make_track(track_id, name="Song", type_="track"):
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


def make_page(entries, offset=0, total=None, limit=2):
    total = len(entries) if total is None else total
    return {
        "items": entries,
        "limit": limit,
        "total": total,
        "next": "next-url" if offset + limit < total else None,
    }


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
    session = mock.Mock()
    session.playlist_items.side_effect = lambda playlist_id, fields, offset, additional_types: pages[offset]

    tracks = asyncio.run(get_tracks_from_spotify_playlist(session, {"id": "pl", "name": "Playlist"}))

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

    tracks = asyncio.run(get_tracks_from_spotify_playlist(session, {"id": "pl", "name": "Playlist"}))

    assert [t["id"] for t in tracks] == ["a", "legacy"]


# Test repeat_on_request_error
import pytest
import requests
import spotipy
import tidalapi
from spotify_to_tidal import sync as _sync
from spotify_to_tidal.sync import _is_transient_error, repeat_on_request_error


def spotify_error(status, reason=None, headers=None):
    return spotipy.exceptions.SpotifyException(status, -1, "error", reason=reason, headers=headers)


def http_error(status, headers=None):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    return requests.exceptions.HTTPError(response=response)


@pytest.fixture
def sleeps(mocker):
    """ Replaces asyncio.sleep in sync.py and records requested durations """
    recorded = []

    async def fake_sleep(seconds):
        recorded.append(seconds)

    mocker.patch.object(_sync.asyncio, "sleep", fake_sleep)
    return recorded


def failing_then(results):
    """ Async function that raises/returns each item of results in turn """
    calls = []

    async def function(*args, **kwargs):
        calls.append(args)
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
    (tidalapi.exceptions.TooManyRequests(), True),
])
def test_is_transient_error(error, expected):
    assert _is_transient_error(error) is expected


def test_non_transient_error_is_raised_without_retrying(sleeps):
    function, calls = failing_then([spotify_error(403)])
    with pytest.raises(spotipy.exceptions.SpotifyException):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 1
    assert sleeps == []


def test_quota_exceeded_is_not_retried(sleeps):
    function, calls = failing_then([spotify_error(429, reason="QUOTA_EXCEEDED")])
    with pytest.raises(spotipy.exceptions.SpotifyException):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 1


def test_transient_error_is_retried_until_success(sleeps):
    function, calls = failing_then([requests.exceptions.ConnectionError(), spotify_error(503), "ok"])
    assert asyncio.run(repeat_on_request_error(function)) == "ok"
    assert len(calls) == 3
    assert sleeps == [1, 10]  # default back-off schedule


def test_retry_after_header_is_respected(sleeps):
    function, _ = failing_then([spotify_error(429, headers={"Retry-After": "7"}), "ok"])
    assert asyncio.run(repeat_on_request_error(function)) == "ok"
    assert sleeps == [7]


def test_tidal_retry_after_is_respected(sleeps):
    function, _ = failing_then([tidalapi.exceptions.TooManyRequests(retry_after=3), "ok"])
    assert asyncio.run(repeat_on_request_error(function)) == "ok"
    assert sleeps == [3]


def test_gives_up_after_retries_are_exhausted(sleeps):
    function, calls = failing_then([spotify_error(500)] * 6)
    with pytest.raises(SystemExit):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 6


def test_gives_up_when_asked_to_wait_too_long(sleeps):
    function, calls = failing_then([spotify_error(429, headers={"Retry-After": "86400"}), "ok"])
    with pytest.raises(SystemExit):
        asyncio.run(repeat_on_request_error(function))
    assert len(calls) == 1
    assert sleeps == []


# Test sync_playlists_wrapper
def test_inaccessible_playlist_is_skipped(mocker):
    synced = []

    async def fake_sync_playlist(spotify_session, tidal_session, spotify_playlist, tidal_playlist, config):
        if spotify_playlist["id"] == "not-mine":
            raise spotify_error(403)
        synced.append(spotify_playlist["id"])

    mocker.patch.object(_sync, "sync_playlist", fake_sync_playlist)
    playlists = [({"id": "not-mine", "name": "Not mine"}, None), ({"id": "mine", "name": "Mine"}, None)]
    _sync.sync_playlists_wrapper(None, None, playlists, {})
    assert synced == ["mine"]


def test_other_spotify_errors_still_abort(mocker):
    async def fake_sync_playlist(*args):
        raise spotify_error(400)

    mocker.patch.object(_sync, "sync_playlist", fake_sync_playlist)
    with pytest.raises(spotipy.exceptions.SpotifyException):
        _sync.sync_playlists_wrapper(None, None, [({"id": "x", "name": "X"}, None)], {})
