# tests/unit/web/test_settings_and_users.py

import pytest
from pathlib import Path
from pytest_mock import MockerFixture

from spotify_to_tidal.cache import MatchFailureDatabase
from spotify_to_tidal.type import SyncConfig
from spotify_to_tidal.web import __main__ as web_main
from spotify_to_tidal.web.settings import WebConfigError, WebSettings
from spotify_to_tidal.web.users import UserData

ENV = {"STT_INVITE_CODE": "let-me-in-please", "STT_SESSION_SECRET": "x" * 32}
CONFIG: SyncConfig = {"spotify": {"client_id": "", "client_secret": "", "username": "", "redirect_uri": ""}}


# Test WebSettings
def test_settings_from_env_defaults():
    settings = WebSettings.from_env(ENV)
    assert settings.invite_code == "let-me-in-please"
    assert settings.data_dir == Path("data")
    assert settings.https_only is False


def test_settings_from_env_overrides():
    settings = WebSettings.from_env({**ENV, "STT_DATA_DIR": "/srv/data", "STT_HTTPS_ONLY": "True"})
    assert settings.data_dir == Path("/srv/data")
    assert settings.https_only is True


@pytest.mark.parametrize("missing", ["STT_INVITE_CODE", "STT_SESSION_SECRET"])
def test_settings_require_secrets(missing: str):
    env = {name: value for name, value in ENV.items() if name != missing}
    with pytest.raises(WebConfigError, match=missing):
        WebSettings.from_env(env)


@pytest.mark.parametrize("name, value", [("STT_INVITE_CODE", "short"), ("STT_SESSION_SECRET", "too-short")])
def test_settings_reject_weak_secrets(name: str, value: str):
    with pytest.raises(WebConfigError, match="at least"):
        WebSettings.from_env({**ENV, name: value})


def test_web_main_exits_with_config_error(mocker: MockerFixture):
    mocker.patch.dict("os.environ", {}, clear=True)
    run = mocker.patch.object(web_main.uvicorn, "run")
    with pytest.raises(SystemExit) as exit_info:
        web_main.main()
    assert "Missing environment variables" in str(exit_info.value.code)
    run.assert_not_called()


def test_web_main_runs_app(mocker: MockerFixture):
    mocker.patch.dict("os.environ", {**ENV, "STT_PORT": "9000"}, clear=True)
    run = mocker.patch.object(web_main.uvicorn, "run")
    web_main.main()
    assert run.call_args.kwargs == {"host": "127.0.0.1", "port": 9000}


# Test UserData
def test_user_data_keeps_everything_in_the_users_directory(tmp_path: Path):
    user = UserData(tmp_path, "spotify_user-1")
    assert user.directory == tmp_path / "spotify_user-1"
    assert not user.directory.exists()  # created on first use

    context = user.sync_context(CONFIG)
    assert isinstance(context.failure_cache, MatchFailureDatabase)
    assert (user.directory / ".cache.db").exists()
    assert user.tidal_session_store().path == user.directory / ".session.yml"
    user.spotify_token_cache().save_token_to_cache({"access_token": "a"})
    assert (user.directory / ".spotify_token").exists()


def test_users_are_kept_apart(tmp_path: Path):
    UserData(tmp_path, "alice").tidal_session_store().save(
        {"token_type": "Bearer", "access_token": "alice", "refresh_token": None, "session_id": None})
    assert UserData(tmp_path, "bob").tidal_session_store().load() is None


@pytest.mark.parametrize("user_id", ["", "../escape", "a/b", "a\\b", "..", "x" * 65, "name with spaces"])
def test_user_data_rejects_unsafe_ids(tmp_path: Path, user_id: str):
    with pytest.raises(ValueError):
        UserData(tmp_path, user_id)
