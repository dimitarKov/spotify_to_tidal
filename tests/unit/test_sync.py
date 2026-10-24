# tests/unit/test_sync.py
# pyright: reportPrivateUsage=false

import asyncio
import pytest
import requests
import spotipy
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Sequence
from unittest import mock
from pytest_mock import MockerFixture
from tidalapi.exceptions import TooManyRequests

from spotify_to_tidal import sync as _sync
from spotify_to_tidal.sync import _extract_track, _is_transient_error, get_tracks_from_spotify_playlist, repeat_on_request_error
from spotify_to_tidal.rate_limiter import RateLimiter
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


# Test tidal_search
from types import SimpleNamespace
from tidalapi.album import Album
from tidalapi.exceptions import InvalidISRC, ObjectNotFound
from tidalapi.media import Track


def tidal_track(isrc: str, name: str = "Song", available: bool = True) -> SimpleNamespace:
    return SimpleNamespace(isrc=isrc, id=hash(isrc), name=name, version=None, duration=180, available=available,
                           artists=[SimpleNamespace(name="Artist")])


def tidal_album(num_tracks: int, tracks: List[SimpleNamespace] | Exception) -> SimpleNamespace:
    def get_tracks() -> List[SimpleNamespace]:
        if isinstance(tracks, Exception):
            raise tracks
        return tracks
    return SimpleNamespace(name="Album", num_tracks=num_tracks, artists=[SimpleNamespace(name="Artist")], tracks=get_tracks)


def fake_tidal_session(albums: List[SimpleNamespace], tracks: List[SimpleNamespace]) -> mock.Mock:
    def search(query: str, models: List[type]) -> Dict[str, Any]:
        return {"albums": albums} if models == [Album] else {"tracks": tracks}
    session = mock.Mock()
    session.search.side_effect = search
    return session


@pytest.fixture
def failure_cache(mocker: MockerFixture) -> mock.Mock:
    return mocker.patch.object(_sync, "failure_cache")


def run_tidal_search(spotify_track: SpotifyTrack, session: mock.Mock, isrc_lookup: _sync.IsrcLookup | None = None) -> Track | None:
    return asyncio.run(_sync.tidal_search(spotify_track, RateLimiter(rate=10, capacity=10), session, isrc_lookup))


def test_tidal_search_skips_unavailable_album(failure_cache: mock.Mock):
    wanted = tidal_track("ISRCa")
    session = fake_tidal_session(
        albums=[tidal_album(1, ObjectNotFound("Object not found")), tidal_album(1, [wanted])],
        tracks=[])
    assert run_tidal_search(make_track("a"), session) is wanted
    failure_cache.remove_match_failure.assert_called_once_with("a")


def test_tidal_search_falls_back_to_track_search_when_album_metadata_is_wrong(failure_cache: mock.Mock):
    wanted = tidal_track("ISRCa")
    spotify_track = make_track("a")
    spotify_track["track_number"] = 5
    # the album claims 10 tracks but only returns 2
    session = fake_tidal_session(albums=[tidal_album(10, [tidal_track("x"), tidal_track("y")])], tracks=[wanted])
    assert run_tidal_search(spotify_track, session) is wanted


def test_tidal_search_records_failure_when_nothing_matches(failure_cache: mock.Mock):
    session = fake_tidal_session(albums=[tidal_album(1, ObjectNotFound("Object not found"))], tracks=[tidal_track("other", name="Other Tune")])
    assert run_tidal_search(make_track("a"), session) is None
    failure_cache.cache_match_failure.assert_called_once_with("a")


# Test search_new_tracks_on_tidal's "songs not found.txt"


@pytest.fixture
def in_tmp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_search_new_tracks(mocker: MockerFixture, found: Dict[str, Any], tracks: List[SpotifyTrack]):
    """ Runs search_new_tracks_on_tidal with tidal_search returning found[spotify_id] (None if missing) """
    async def fake_tidal_search(spotify_track: SpotifyTrack, *args: Any) -> Any:
        return found.get(spotify_track["id"] or "")

    def all_tracks_are_new(spotify_tracks: Sequence[SpotifyTrack]) -> List[SpotifyTrack]:
        return list(spotify_tracks)

    mocker.patch.object(_sync, "tidal_search", fake_tidal_search)
    mocker.patch.object(_sync, "get_new_spotify_tracks", all_tracks_are_new)
    mocker.patch.object(_sync, "track_match_cache")
    asyncio.run(_sync.search_new_tracks_on_tidal(mock.Mock(), tracks, "My Playlist", EMPTY_CONFIG))


def test_not_found_file_lists_missing_tracks(mocker: MockerFixture, in_tmp_dir: Path):
    run_search_new_tracks(mocker, found={"a": SimpleNamespace(id=1)}, tracks=[make_track("a"), make_track("b", name="Lost")])
    content = (in_tmp_dir / "songs not found.txt").read_text(encoding="utf-8")
    assert "Playlist: My Playlist" in content
    assert "b: Artist - Lost" in content
    assert "a: " not in content


def test_not_found_file_not_written_when_everything_is_found(mocker: MockerFixture, in_tmp_dir: Path):
    run_search_new_tracks(mocker, found={"a": SimpleNamespace(id=1)}, tracks=[make_track("a")])
    assert not (in_tmp_dir / "songs not found.txt").exists()


# Test ISRC lookup (I1)
def test_tidal_search_finds_track_by_isrc_without_text_search(failure_cache: mock.Mock):
    wanted = tidal_track("ISRCa", name="Spelled Differently On Tidal")
    session = fake_tidal_session(albums=[], tracks=[])
    session.get_tracks_by_isrc.return_value = [wanted]
    assert run_tidal_search(make_track("a"), session, _sync.IsrcLookup(session)) is wanted
    session.get_tracks_by_isrc.assert_called_once_with("ISRCa")
    session.search.assert_not_called()
    failure_cache.remove_match_failure.assert_called_once_with("a")


def test_tidal_search_prefers_available_isrc_result(failure_cache: mock.Mock):
    unavailable, available = tidal_track("ISRCa", available=False), tidal_track("ISRCa")
    session = fake_tidal_session(albums=[], tracks=[])
    session.get_tracks_by_isrc.return_value = [unavailable, available]
    assert run_tidal_search(make_track("a"), session, _sync.IsrcLookup(session)) is available


@pytest.mark.parametrize("isrc_result", [
    [tidal_track("ISRCa", available=False)],  # only unavailable results
    ObjectNotFound(),  # tidalapi raises this when no track has the ISRC
    InvalidISRC(),  # any HTTP error from the endpoint
])
def test_tidal_search_falls_back_to_text_search(failure_cache: mock.Mock, isrc_result: List[SimpleNamespace] | Exception):
    wanted = tidal_track("ISRCa")
    session = fake_tidal_session(albums=[], tracks=[wanted])
    # an exception is raised; a list of return values is returned one call at a time
    session.get_tracks_by_isrc.side_effect = isrc_result if isinstance(isrc_result, Exception) else [isrc_result]
    assert run_tidal_search(make_track("a"), session, _sync.IsrcLookup(session)) is wanted
    session.search.assert_called()


def test_tidal_search_skips_isrc_lookup_without_isrc(failure_cache: mock.Mock):
    spotify_track = make_track("a")
    del spotify_track["external_ids"]
    wanted = tidal_track("other")
    session = fake_tidal_session(albums=[], tracks=[wanted])
    assert run_tidal_search(spotify_track, session, _sync.IsrcLookup(session)) is wanted
    session.get_tracks_by_isrc.assert_not_called()


def test_isrc_lookup_disables_itself_after_repeated_errors():
    session = mock.Mock()
    session.get_tracks_by_isrc.side_effect = InvalidISRC()
    lookup = _sync.IsrcLookup(session)
    for _ in range(_sync.IsrcLookup.MAX_CONSECUTIVE_ERRORS + 3):
        assert asyncio.run(lookup.find("ISRC")) == []
    assert not lookup.enabled
    assert session.get_tracks_by_isrc.call_count == _sync.IsrcLookup.MAX_CONSECUTIVE_ERRORS


def test_isrc_lookup_error_count_resets_on_success():
    session = mock.Mock()
    errors = [InvalidISRC()] * (_sync.IsrcLookup.MAX_CONSECUTIVE_ERRORS - 1)
    session.get_tracks_by_isrc.side_effect = errors + [[]] + errors
    lookup = _sync.IsrcLookup(session)
    for _ in range(2 * len(errors) + 1):
        asyncio.run(lookup.find("ISRC"))
    assert lookup.enabled
