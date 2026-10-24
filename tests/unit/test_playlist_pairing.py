# tests/unit/test_playlist_pairing.py

import asyncio
from pathlib import Path
from typing import Any, List, Set
from unittest import mock
from pytest_mock import MockerFixture
from tidalapi.playlist import Playlist, UserPlaylist
from tidalapi.user import LoggedInUser

from spotify_to_tidal import sync as _sync
from spotify_to_tidal.context import SyncContext
from spotify_to_tidal.storage import FilePlaylistMappingStore, InMemoryPlaylistMappingStore
from spotify_to_tidal.type import SpotifyPlaylist, SyncConfig

CONFIG: SyncConfig = {"spotify": {"client_id": "", "client_secret": "", "username": "", "redirect_uri": ""}}


def spotify_playlist(playlist_id: str, name: str) -> SpotifyPlaylist:
    return {"id": playlist_id, "name": name, "description": None, "owner": {"id": "me"}}


def tidal_playlist(playlist_id: str, name: str, owned: bool = True) -> Any:
    playlist = mock.Mock(spec=UserPlaylist if owned else Playlist)
    playlist.id, playlist.name = playlist_id, name
    return playlist


def pick(mappings: InMemoryPlaylistMappingStore, playlist: SpotifyPlaylist, tidal_playlists: List[Any], claimed: Set[str] | None = None) -> Any:
    return _sync.pick_tidal_playlist_for_spotify_playlist(mappings, playlist, tidal_playlists, claimed)[1]


# Test pick_tidal_playlist_for_spotify_playlist
def test_pairs_by_name_when_never_synced():
    road_trip = tidal_playlist("t1", "Road Trip")
    assert pick(InMemoryPlaylistMappingStore(), spotify_playlist("s1", "Road Trip"), [tidal_playlist("t0", "Other"), road_trip]) is road_trip


def test_no_match_creates_new():
    assert pick(InMemoryPlaylistMappingStore(), spotify_playlist("s1", "Road Trip"), [tidal_playlist("t0", "Other")]) is None


def test_stored_pairing_survives_renames():
    renamed_on_tidal = tidal_playlist("t1", "Summer Road Trip")
    same_name = tidal_playlist("t2", "Road Trip")
    mappings = InMemoryPlaylistMappingStore({"s1": "t1"})
    assert pick(mappings, spotify_playlist("s1", "Road Trip"), [same_name, renamed_on_tidal]) is renamed_on_tidal


def test_stored_pairing_to_deleted_playlist_falls_back_to_name():
    road_trip = tidal_playlist("t2", "Road Trip")
    mappings = InMemoryPlaylistMappingStore({"s1": "deleted"})
    assert pick(mappings, spotify_playlist("s1", "Road Trip"), [road_trip]) is road_trip


def test_name_match_skips_playlist_paired_with_another_spotify_playlist():
    # s2 was renamed on Spotify to the name of t1, which belongs to s1
    mappings = InMemoryPlaylistMappingStore({"s1": "t1"})
    assert pick(mappings, spotify_playlist("s2", "Road Trip"), [tidal_playlist("t1", "Road Trip")]) is None


def test_name_match_skips_playlists_not_owned():
    followed = tidal_playlist("t1", "Road Trip", owned=False)
    assert pick(InMemoryPlaylistMappingStore(), spotify_playlist("s1", "Road Trip"), [followed]) is None


def test_two_spotify_playlists_with_the_same_name_get_different_tidal_playlists():
    existing = tidal_playlist("t1", "Mix")
    mappings, claimed = InMemoryPlaylistMappingStore(), set[str]()
    assert pick(mappings, spotify_playlist("s1", "Mix"), [existing], claimed) is existing
    assert pick(mappings, spotify_playlist("s2", "Mix"), [existing], claimed) is None  # a new one will be created
    assert claimed == {"t1"}


def test_get_user_playlist_mappings_shares_claims_across_playlists(mocker: MockerFixture):
    async def spotify_playlists(*args: Any) -> List[SpotifyPlaylist]:
        return [spotify_playlist("s1", "Mix"), spotify_playlist("s2", "Mix")]

    existing = tidal_playlist("t1", "Mix")

    def tidal_playlists(tidal_session: Any) -> List[Any]:
        return [existing]

    mocker.patch.object(_sync, "get_playlists_from_spotify", spotify_playlists)
    mocker.patch.object(_sync, "get_tidal_playlists_wrapper", tidal_playlists)
    pairs = _sync.get_user_playlist_mappings(SyncContext(CONFIG), mock.Mock(), mock.Mock())
    assert [tidal for _, tidal in pairs] == [existing, None]


# Test that sync_playlist records the pairing
def run_sync_playlist(mocker: MockerFixture, context: SyncContext, tidal: Any, tidal_session: Any):
    async def tracks(*args: Any) -> List[Any]:
        return [{"id": "track"}]

    async def no_tidal_tracks(*args: Any) -> List[Any]:
        return []

    async def no_search(*args: Any) -> None:
        return None

    mocker.patch.object(_sync, "get_tracks_from_spotify_playlist", tracks)
    mocker.patch.object(_sync, "get_all_playlist_tracks", no_tidal_tracks)
    mocker.patch.object(_sync, "search_new_tracks_on_tidal", no_search)
    mocker.patch.object(_sync, "populate_track_match_cache")
    mocker.patch.object(_sync, "get_tracks_for_new_tidal_playlist", return_value=[])
    asyncio.run(_sync.sync_playlist(context, mock.Mock(), tidal_session, spotify_playlist("s1", "Road Trip"), tidal))


def test_sync_playlist_records_pairing_for_new_playlist(mocker: MockerFixture):
    tidal_session = mock.Mock()
    tidal_session.user = mock.Mock(spec=LoggedInUser)
    tidal_session.user.create_playlist.return_value = tidal_playlist("new", "Road Trip")
    context = SyncContext(CONFIG)
    run_sync_playlist(mocker, context, None, tidal_session)
    tidal_session.user.create_playlist.assert_called_once_with("Road Trip", "")
    assert context.playlist_mappings.get("s1") == "new"


def test_sync_playlist_records_pairing_for_existing_playlist(mocker: MockerFixture):
    context = SyncContext(CONFIG)
    run_sync_playlist(mocker, context, tidal_playlist("t1", "Road Trip"), mock.Mock())
    assert context.playlist_mappings.get("s1") == "t1"


# Test the mapping stores
def test_file_playlist_mapping_store_round_trip(tmp_path: Path):
    store = FilePlaylistMappingStore(tmp_path / ".playlist_mappings.yml")
    assert store.get("s1") is None
    assert store.all() == {}
    store.set("s1", "t1")
    store.set("s2", "t2")
    store.set("s1", "t3")
    reopened = FilePlaylistMappingStore(tmp_path / ".playlist_mappings.yml")
    assert reopened.all() == {"s1": "t3", "s2": "t2"}
    assert reopened.get("s2") == "t2"


def test_in_memory_playlist_mapping_store_all_is_a_copy():
    store = InMemoryPlaylistMappingStore({"s1": "t1"})
    store.all()["s2"] = "t2"
    assert store.all() == {"s1": "t1"}


def test_local_files_context_uses_file_mapping_store(tmp_path: Path):
    context = SyncContext.local_files(CONFIG, tmp_path)
    assert isinstance(context.playlist_mappings, FilePlaylistMappingStore)
    assert context.playlist_mappings.path == tmp_path / ".playlist_mappings.yml"
