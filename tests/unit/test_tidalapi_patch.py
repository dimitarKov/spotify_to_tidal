# tests/unit/test_tidalapi_patch.py
# pyright: reportPrivateUsage=false

import asyncio
import pytest
import requests
from typing import Any, Dict, List, Mapping, Sequence, Tuple, cast
from unittest import mock
from pytest_mock import MockerFixture
from tidalapi.playlist import UserPlaylist

from conftest import RecordingReporter
from spotify_to_tidal.tidalapi_patch import _get_all_chunks, add_multiple_tracks_to_playlist, clear_tidal_playlist


def http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


class FakeRequests:
    """ Stands in for tidalapi's Requests; only DELETE is used by tidalapi_patch """

    def __init__(self, playlist: "FakePlaylist"):
        self.playlist = playlist

    def request(self, method: str, path: str, headers: Dict[str, str]):
        error = self.playlist.next_error()
        if error:
            raise error
        self.playlist.deletes.append((path, dict(headers)))
        self.playlist.num_tracks -= len(path.rsplit('/', 1)[-1].split(','))


class FakePlaylist:
    """ Stands in for tidalapi's UserPlaylist. Each write consumes the next scripted error, if any """

    def __init__(self, num_tracks: int = 0, errors: Sequence[Exception | None] = ()):
        self.id = "pl"
        self._base_url = "playlists/%s"
        self._etag: str | None = "v0"
        self.num_tracks = num_tracks
        self.errors = list(errors)
        self.reparses = 0
        self.added: List[List[str]] = []
        self.deletes: List[Tuple[str, Mapping[str, str]]] = []
        self.request = FakeRequests(self)

    def next_error(self) -> Exception | None:
        return self.errors.pop(0) if self.errors else None

    def _reparse(self):
        self.reparses += 1
        self._etag = f"v{self.reparses}"

    def add(self, media_ids: List[str]) -> List[int]:
        error = self.next_error()
        if error:
            raise error
        self.added.append(media_ids)
        self.num_tracks += len(media_ids)
        self._reparse()
        return []


def as_user_playlist(playlist: FakePlaylist) -> UserPlaylist:
    return cast(UserPlaylist, playlist)


@pytest.fixture
def sleeps(mocker: MockerFixture) -> List[float]:
    recorded: List[float] = []
    mocker.patch("time.sleep", recorded.append)
    return recorded


def test_add_retries_on_412_with_refreshed_etag(sleeps: List[float]):
    playlist = FakePlaylist(errors=[http_error(412), http_error(412)])
    add_multiple_tracks_to_playlist(as_user_playlist(playlist), [1, 2, 3])
    assert playlist.added == [["1", "2", "3"]]
    assert sleeps == [1, 2]  # exponential back-off
    assert playlist.reparses == 3  # two ETag refreshes + tidalapi's own after the successful add


def test_add_does_not_retry_other_errors(sleeps: List[float]):
    playlist = FakePlaylist(errors=[http_error(500)])
    with pytest.raises(requests.HTTPError):
        add_multiple_tracks_to_playlist(as_user_playlist(playlist), [1])
    assert sleeps == []


def test_add_gives_up_after_five_attempts(sleeps: List[float]):
    playlist = FakePlaylist(errors=[http_error(412)] * 5)
    with pytest.raises(requests.HTTPError):
        add_multiple_tracks_to_playlist(as_user_playlist(playlist), [1])
    assert playlist.added == []
    assert sleeps == [1, 2, 4, 8]


def test_add_in_chunks(sleeps: List[float], reporter: RecordingReporter):
    playlist = FakePlaylist()
    add_multiple_tracks_to_playlist(as_user_playlist(playlist), list(range(45)), chunk_size=20)
    assert [len(chunk) for chunk in playlist.added] == [20, 20, 5]
    assert reporter.progress_summary() == [("Adding new tracks to Tidal playlist", 45, 45)]


def test_clear_retries_delete_with_refreshed_etag(sleeps: List[float]):
    playlist = FakePlaylist(num_tracks=3, errors=[http_error(412)])
    clear_tidal_playlist(as_user_playlist(playlist))
    assert playlist.num_tracks == 0
    assert len(playlist.deletes) == 1
    path, headers = playlist.deletes[0]
    assert path == "playlists/pl/items/0,1,2"
    assert headers == {"If-None-Match": "v1"}  # the refreshed ETag, not the stale "v0"
    assert sleeps == [1]


def test_clear_in_chunks(sleeps: List[float], reporter: RecordingReporter):
    playlist = FakePlaylist(num_tracks=45)
    clear_tidal_playlist(as_user_playlist(playlist), chunk_size=20)
    assert reporter.progress_summary() == [("Erasing existing tracks from Tidal playlist", 45, 45)]
    assert playlist.num_tracks == 0
    assert len(playlist.deletes) == 3
    assert sleeps == []


# Test _get_all_chunks


def fake_paged_session(total: int) -> tuple[mock.Mock, List[Mapping[str, Any]]]:
    """ Session whose endpoint serves items 0..total-1, 10 per page. Records the params object of every request """
    requested: List[Mapping[str, Any]] = []

    def map_request(url: str, params: Mapping[str, Any]) -> Dict[str, Any]:
        requested.append(params)  # keep the object itself, to detect it being mutated later
        offset = params['offset']
        return {'limit': 10, 'totalNumberOfItems': total, 'items': list(range(offset, min(offset + 10, total)))}

    def map_json(json_obj: Dict[str, Any], parse: Any) -> List[Any]:
        return [parse(item) for item in json_obj['items']]

    session = mock.Mock()
    session.request.map_request.side_effect = map_request
    session.request.map_json.side_effect = map_json
    return session, requested


def test_get_all_chunks_fetches_every_page_once():
    session, requested = fake_paged_session(total=35)
    items = asyncio.run(_get_all_chunks("url", session, parser=lambda item: item, params={'limit': 10}))
    assert sorted(items) == list(range(35))
    # each request must keep its own offset; a shared, mutated params dict would show the same offset everywhere
    assert sorted(params['offset'] for params in requested) == [0, 10, 20, 30]
    assert all(params['limit'] == 10 for params in requested)


def test_get_all_chunks_does_not_modify_callers_params():
    session, _ = fake_paged_session(total=15)
    params = {'limit': 10}
    asyncio.run(_get_all_chunks("url", session, parser=lambda item: item, params=params))
    assert params == {'limit': 10}
