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
