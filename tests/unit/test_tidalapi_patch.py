# tests/unit/test_tidalapi_patch.py

import pytest
import requests
from typing import Dict, List, Mapping, Sequence, Tuple, cast
from pytest_mock import MockerFixture
from tidalapi.playlist import UserPlaylist

from spotify_to_tidal.tidalapi_patch import add_multiple_tracks_to_playlist, clear_tidal_playlist


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


def test_add_in_chunks(sleeps: List[float]):
    playlist = FakePlaylist()
    add_multiple_tracks_to_playlist(as_user_playlist(playlist), list(range(45)), chunk_size=20)
    assert [len(chunk) for chunk in playlist.added] == [20, 20, 5]


def test_clear_retries_delete_with_refreshed_etag(sleeps: List[float]):
    playlist = FakePlaylist(num_tracks=3, errors=[http_error(412)])
    clear_tidal_playlist(as_user_playlist(playlist))
    assert playlist.num_tracks == 0
    assert len(playlist.deletes) == 1
    path, headers = playlist.deletes[0]
    assert path == "playlists/pl/items/0,1,2"
    assert headers == {"If-None-Match": "v1"}  # the refreshed ETag, not the stale "v0"
    assert sleeps == [1]


def test_clear_in_chunks(sleeps: List[float]):
    playlist = FakePlaylist(num_tracks=45)
    clear_tidal_playlist(as_user_playlist(playlist), chunk_size=20)
    assert playlist.num_tracks == 0
    assert len(playlist.deletes) == 3
    assert sleeps == []
