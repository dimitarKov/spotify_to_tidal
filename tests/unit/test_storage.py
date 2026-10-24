# tests/unit/test_storage.py
# pyright: reportPrivateUsage=false

import datetime
import os
import subprocess
import sys
from pathlib import Path

from spotify_to_tidal.cache import InMemoryMatchFailureStore, MatchFailureDatabase, TrackMatchCache
from spotify_to_tidal.context import SyncContext
from spotify_to_tidal.storage import (FileNotFoundReport, FileTidalSessionStore, InMemoryNotFoundReport,
                                      InMemoryTidalSessionStore, TidalTokens)
from spotify_to_tidal.type import SyncConfig

CONFIG: SyncConfig = {"spotify": {"client_id": "", "client_secret": "", "username": "", "redirect_uri": ""}}
TOKENS: TidalTokens = {"token_type": "Bearer", "access_token": "access", "refresh_token": "refresh", "session_id": None}


def test_file_not_found_report_appends_sections(tmp_path: Path):
    report = FileNotFoundReport(tmp_path / "songs not found.txt")
    report.record("First", ["a: Artist - Song\n    no candidates found on Tidal"])
    report.record("Empty", [])
    report.record("Second", ["b: Artist - Other"])
    assert (tmp_path / "songs not found.txt").read_text(encoding="utf-8") == (
        "==========================\nPlaylist: First\n==========================\n"
        "a: Artist - Song\n    no candidates found on Tidal\n"
        "==========================\nPlaylist: Second\n==========================\n"
        "b: Artist - Other\n")


def test_in_memory_not_found_report():
    report = InMemoryNotFoundReport()
    report.record("Playlist", ["a"])
    report.record("Empty", [])
    report.record("Playlist", ["b"])
    assert report.playlists == {"Playlist": ["a", "b"]}


def test_file_tidal_session_store_round_trip(tmp_path: Path):
    store = FileTidalSessionStore(tmp_path / ".session.yml")
    assert store.load() is None
    store.save(TOKENS)
    assert FileTidalSessionStore(tmp_path / ".session.yml").load() == TOKENS


def test_in_memory_tidal_session_store():
    store = InMemoryTidalSessionStore()
    assert store.load() is None
    store.save(TOKENS)
    assert store.load() == TOKENS


def test_in_memory_match_failure_store_back_off():
    store = InMemoryMatchFailureStore()
    assert not store.has_match_failure("track")
    store.cache_match_failure("track")
    assert store.has_match_failure("track")
    first_failed, next_retry = store._failures["track"]
    assert abs((next_retry - datetime.datetime.now()) / datetime.timedelta(days=1) - 7) < 0.01
    store.cache_match_failure("track")
    assert store._failures["track"][0] == first_failed
    store.remove_match_failure("track")
    assert not store.has_match_failure("track")
    store.remove_match_failure("missing")  # no error


def test_track_match_caches_are_independent():
    first, second = TrackMatchCache(), TrackMatchCache()
    first.insert(("spotify_id", 1))
    assert second.get("spotify_id") is None


def test_sync_context_defaults_are_in_memory_and_separate():
    first, second = SyncContext(CONFIG), SyncContext(CONFIG)
    assert isinstance(first.failure_cache, InMemoryMatchFailureStore)
    assert isinstance(first.not_found, InMemoryNotFoundReport)
    assert first.failure_cache is not second.failure_cache
    assert first.track_match_cache is not second.track_match_cache


def test_sync_context_local_files(tmp_path: Path):
    context = SyncContext.local_files(CONFIG, tmp_path)
    assert isinstance(context.failure_cache, MatchFailureDatabase)
    assert (tmp_path / ".cache.db").exists()
    assert isinstance(context.not_found, FileNotFoundReport)
    assert context.not_found.path == tmp_path / "songs not found.txt"


def test_importing_the_library_creates_no_files(tmp_path: Path):
    """ The CLI used to create .cache.db in the current directory just by importing the cache module """
    src = Path(__file__).parents[2] / "src"
    subprocess.run([sys.executable, "-c", "import spotify_to_tidal.sync, spotify_to_tidal.auth, spotify_to_tidal.__main__"],
                   cwd=tmp_path, check=True, env={"PYTHONPATH": str(src), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")})
    assert list(tmp_path.iterdir()) == []
