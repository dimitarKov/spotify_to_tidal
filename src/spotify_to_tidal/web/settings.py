import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class WebConfigError(ValueError):
    """ The web app's environment variables are missing or invalid """


MIN_INVITE_CODE_LENGTH = 8
MIN_SESSION_SECRET_LENGTH = 32

_TRUE = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class WebSettings:
    """ Configuration for the web app, read from environment variables (see from_env) """
    invite_code: str
    session_secret: str
    data_dir: Path = Path("data")
    https_only: bool = False  # mark the session cookie Secure; enable when served over HTTPS

    def __post_init__(self):
        if len(self.invite_code) < MIN_INVITE_CODE_LENGTH:
            raise WebConfigError(f"The invite code must be at least {MIN_INVITE_CODE_LENGTH} characters")
        if len(self.session_secret) < MIN_SESSION_SECRET_LENGTH:
            raise WebConfigError(f"The session secret must be at least {MIN_SESSION_SECRET_LENGTH} characters")

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "WebSettings":
        """ STT_INVITE_CODE (required): the code friends enter to use the app
            STT_SESSION_SECRET (required): random secret that signs session cookies,
                e.g. python -c "import secrets; print(secrets.token_urlsafe(32))"
            STT_DATA_DIR: where each user's data is kept (default: ./data)
            STT_HTTPS_ONLY: "true" to only send the session cookie over HTTPS """
        missing = [name for name in ("STT_INVITE_CODE", "STT_SESSION_SECRET") if not env.get(name)]
        if missing:
            raise WebConfigError(f"Missing environment variables: {', '.join(missing)}")
        return cls(invite_code=env["STT_INVITE_CODE"],
                   session_secret=env["STT_SESSION_SECRET"],
                   data_dir=Path(env.get("STT_DATA_DIR") or "data"),
                   https_only=env.get("STT_HTTPS_ONLY", "").lower() in _TRUE)
