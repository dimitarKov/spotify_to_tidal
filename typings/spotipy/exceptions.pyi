from typing import Any, Mapping

class SpotifyBaseException(Exception): ...

class SpotifyException(SpotifyBaseException):
    http_status: int
    code: int
    msg: str
    reason: str | None
    headers: Mapping[str, Any]
    def __init__(
        self,
        http_status: int,
        code: int,
        msg: str,
        reason: str | None = None,
        headers: Mapping[str, Any] | None = None,
    ) -> None: ...

class SpotifyOauthError(SpotifyBaseException):
    error: str | None
    error_description: str | None
    def __init__(self, message: str, error: str | None = None, error_description: str | None = None, *args: Any, **kwargs: Any) -> None: ...
