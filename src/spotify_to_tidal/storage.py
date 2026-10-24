""" Where a user's persistent state lives. Each kind of state has an interface, a local-file implementation
    (used by the CLI) and an in-memory one. A hosted deployment can provide its own (e.g. database-backed) ones """

import yaml
from pathlib import Path
from typing import Dict, List, Optional, Protocol, Sequence, TypedDict


class MatchFailureStore(Protocol):
    """ Spotify tracks that recently failed to match, so they aren't searched for again on every run.
        Implemented by cache.MatchFailureDatabase (SQLite file) and cache.InMemoryMatchFailureStore """

    def cache_match_failure(self, track_id: str) -> None: ...
    def has_match_failure(self, track_id: str) -> bool: ...
    def remove_match_failure(self, track_id: str) -> None: ...


class NotFoundReport(Protocol):
    """ The tracks of each playlist that couldn't be found on Tidal, with an explanation line per track """

    def record(self, playlist_name: str, entries: Sequence[str]) -> None: ...


class FileNotFoundReport:
    """ Appends to a text file, "songs not found.txt" by default """

    def __init__(self, path: str | Path = "songs not found.txt"):
        self.path = Path(path)

    def record(self, playlist_name: str, entries: Sequence[str]) -> None:
        if not entries:
            return
        header = f"==========================\nPlaylist: {playlist_name}\n==========================\n"
        with open(self.path, "a", encoding="utf-8") as file:
            file.write(header)
            for entry in entries:
                file.write(f"{entry}\n")


class InMemoryNotFoundReport:
    def __init__(self):
        self.playlists: Dict[str, List[str]] = {}

    def record(self, playlist_name: str, entries: Sequence[str]) -> None:
        if entries:
            self.playlists.setdefault(playlist_name, []).extend(entries)


class TidalTokens(TypedDict):
    token_type: str
    access_token: str
    refresh_token: Optional[str]
    session_id: Optional[str]


class TidalSessionStore(Protocol):
    """ The user's Tidal login, so they don't have to log in again on every run """

    def load(self) -> TidalTokens | None: ...
    def save(self, tokens: TidalTokens) -> None: ...


class FileTidalSessionStore:
    """ A YAML file, ".session.yml" by default """

    def __init__(self, path: str | Path = ".session.yml"):
        self.path = Path(path)

    def load(self) -> TidalTokens | None:
        try:
            with open(self.path, "r") as session_file:
                tokens: TidalTokens | None = yaml.safe_load(session_file)
                return tokens
        except OSError:
            return None

    def save(self, tokens: TidalTokens) -> None:
        with open(self.path, "w") as session_file:
            yaml.dump(dict(tokens), session_file)


class InMemoryTidalSessionStore:
    def __init__(self, tokens: TidalTokens | None = None):
        self.tokens = tokens

    def load(self) -> TidalTokens | None:
        return self.tokens

    def save(self, tokens: TidalTokens) -> None:
        self.tokens = tokens
