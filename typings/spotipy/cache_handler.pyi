from typing import Any

TokenInfo = dict[str, Any]

class CacheHandler:
    """ Stores the user's Spotify OAuth token between runs """
    def get_cached_token(self) -> TokenInfo | None: ...
    def save_token_to_cache(self, token_info: TokenInfo) -> None: ...

class CacheFileHandler(CacheHandler):
    def __init__(self, cache_path: str | None = None, username: str | None = None, encoder_cls: Any = None) -> None: ...

class MemoryCacheHandler(CacheHandler):
    def __init__(self, token_info: TokenInfo | None = None) -> None: ...
