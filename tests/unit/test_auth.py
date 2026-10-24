# tests/unit/test_auth.py

import pytest
import spotipy
from pytest_mock import MockerFixture
from unittest import mock
from spotify_to_tidal.auth import (SPOTIFY_SCOPES, SpotifyLogin, TidalLoginLink, open_spotify_session, open_tidal_session,
                                   resume_tidal_session, start_tidal_login)
from spotify_to_tidal.storage import InMemoryTidalSessionStore, TidalTokens
from conftest import RecordingReporter
from spotify_to_tidal.errors import SpotifyAuthError, TidalAuthError
from spotify_to_tidal.type import SpotifyConfig


def test_open_spotify_session(mocker: MockerFixture):
    # Mock the SpotifyOAuth class
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mock_spotify_instance = mocker.patch(
        "spotify_to_tidal.auth.spotipy.Spotify", autospec=True
    )

    # Define a mock configuration
    mock_config: SpotifyConfig = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
        "open_browser": True,
    }

    # Create a mock SpotifyOAuth instance
    mock_oauth_instance = mock_spotify_oauth.return_value
    mock_oauth_instance.get_access_token.return_value = "mock_access_token"

    # Call the function under test
    spotify_instance = open_spotify_session(mock_config)

    # Assert that the SpotifyOAuth was called with correct parameters
    mock_spotify_oauth.assert_called_once_with(
        username="test_user",
        scope=SPOTIFY_SCOPES,
        client_id="test_client_id",
        client_secret="test_client_secret",
        redirect_uri="http://127.0.0.1/",
        requests_timeout=10,
        open_browser=True,
        cache_handler=None,
    )

    # Assert that the Spotify instance was created
    mock_spotify_instance.assert_called_once_with(oauth_manager=mock_oauth_instance)
    assert spotify_instance == mock_spotify_instance.return_value


def test_open_spotify_session_oauth_error(mocker: MockerFixture):
    # Mock the SpotifyOAuth class and simulate an OAuth error
    mock_spotify_oauth = mocker.patch(
        "spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True
    )
    mock_spotify_oauth.return_value.get_access_token.side_effect = (
        spotipy.SpotifyOauthError("mock error")
    )

    # Define a mock configuration
    mock_config: SpotifyConfig = {
        "username": "test_user",
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "redirect_uri": "http://127.0.0.1/",
    }

    # Call the function under test and assert a SpotifyAuthError naming the user is raised
    with pytest.raises(SpotifyAuthError, match="could not get token for username: test_user") as error:
        open_spotify_session(mock_config)
    assert isinstance(error.value.__cause__, spotipy.SpotifyOauthError)


def test_open_spotify_session_uses_given_token_cache(mocker: MockerFixture):
    mock_spotify_oauth = mocker.patch("spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True)
    mocker.patch("spotify_to_tidal.auth.spotipy.Spotify", autospec=True)
    config: SpotifyConfig = {"username": "u", "client_id": "c", "client_secret": "s", "redirect_uri": "http://127.0.0.1/"}
    cache = spotipy.MemoryCacheHandler()
    open_spotify_session(config, cache_handler=cache)
    assert mock_spotify_oauth.call_args.kwargs["cache_handler"] is cache


# Test open_tidal_session

TOKENS: TidalTokens = {"token_type": "Bearer", "access_token": "access", "refresh_token": "refresh", "session_id": "session"}


def test_open_tidal_session_reuses_stored_tokens(mocker: MockerFixture):
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    session.load_oauth_session.return_value = True
    store = InMemoryTidalSessionStore(TOKENS)
    assert open_tidal_session(store) is session
    session.load_oauth_session.assert_called_once_with(token_type="Bearer", access_token="access", refresh_token="refresh")
    session.login_oauth.assert_not_called()


def test_open_tidal_session_logs_in_and_saves_tokens(mocker: MockerFixture, reporter: RecordingReporter):
    mocker.patch("spotify_to_tidal.auth.webbrowser")
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    session.login_oauth.return_value = (mock.Mock(verification_uri_complete="link.tidal.com/ABCDE"), mock.Mock())
    session.token_type, session.access_token, session.refresh_token, session.session_id = "Bearer", "new-access", "new-refresh", "new-session"
    store = InMemoryTidalSessionStore()
    assert open_tidal_session(store) is session
    assert store.tokens == {"token_type": "Bearer", "access_token": "new-access", "refresh_token": "new-refresh", "session_id": "new-session"}
    assert reporter.infos == ["Login with the webbrowser: https://link.tidal.com/ABCDE"]


def test_open_tidal_session_login_failure_raises(mocker: MockerFixture):
    mocker.patch("spotify_to_tidal.auth.webbrowser")
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    future = mock.Mock()
    future.result.side_effect = TimeoutError()
    session.login_oauth.return_value = (mock.Mock(verification_uri_complete="https://link.tidal.com/ABCDE"), future)
    store = InMemoryTidalSessionStore()
    with pytest.raises(TidalAuthError):
        open_tidal_session(store)
    assert store.tokens is None


def test_open_tidal_session_uses_given_browser_opener(mocker: MockerFixture):
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    session.login_oauth.return_value = (tidal_link_login(), mock.Mock())
    session.token_type, session.access_token, session.refresh_token, session.session_id = "Bearer", "a", "r", "s"
    opened: list[str] = []
    open_tidal_session(InMemoryTidalSessionStore(), open_browser=opened.append)
    assert opened == ["https://link.tidal.com/ABCDE"]


# Test the step-by-step Tidal login (for a web UI)
def tidal_link_login(url: str = "link.tidal.com/ABCDE") -> mock.Mock:
    return mock.Mock(verification_uri_complete=url, user_code="ABCDE", expires_in=300.0)


def test_resume_tidal_session_without_tokens(mocker: MockerFixture):
    session_class = mocker.patch("spotify_to_tidal.auth.Session", autospec=True)
    assert resume_tidal_session(InMemoryTidalSessionStore()) is None
    session_class.assert_not_called()


@pytest.mark.parametrize("load_result", [False, ValueError("expired")])
def test_resume_tidal_session_with_invalid_tokens(mocker: MockerFixture, reporter: RecordingReporter, load_result: bool | Exception):
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    if isinstance(load_result, Exception):
        session.load_oauth_session.side_effect = load_result
    else:
        session.load_oauth_session.return_value = load_result
    assert resume_tidal_session(InMemoryTidalSessionStore(TOKENS)) is None


def test_start_tidal_login_gives_link_then_session(mocker: MockerFixture):
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    future = mock.Mock()
    future.done.return_value = False
    session.login_oauth.return_value = (tidal_link_login(), future)
    store = InMemoryTidalSessionStore()

    pending = start_tidal_login(store)
    assert pending.link == TidalLoginLink(url="https://link.tidal.com/ABCDE", user_code="ABCDE", expires_in=300.0)
    assert not pending.done()
    assert store.tokens is None

    # the user approves the login
    future.done.return_value = True
    session.token_type, session.access_token, session.refresh_token, session.session_id = "Bearer", "a", "r", "s"
    assert pending.done()
    assert pending.result() is session
    assert store.tokens == {"token_type": "Bearer", "access_token": "a", "refresh_token": "r", "session_id": "s"}


def test_tidal_login_without_token_raises(mocker: MockerFixture):
    session = mocker.patch("spotify_to_tidal.auth.Session", autospec=True).return_value
    session.login_oauth.return_value = (tidal_link_login("https://link.tidal.com/ABCDE"), mock.Mock())
    session.token_type, session.access_token = None, None
    store = InMemoryTidalSessionStore()
    with pytest.raises(TidalAuthError, match="access token"):
        start_tidal_login(store).result()
    assert store.tokens is None


# Test the step-by-step Spotify login (for a web UI)
SPOTIFY_CONFIG: SpotifyConfig = {"username": "u", "client_id": "c", "client_secret": "s", "redirect_uri": "https://app.example/callback"}


@pytest.fixture
def spotify_oauth(mocker: MockerFixture) -> mock.Mock:
    mocker.patch("spotify_to_tidal.auth.spotipy.Spotify", autospec=True)
    return mocker.patch("spotify_to_tidal.auth.spotipy.SpotifyOAuth", autospec=True)


def test_spotify_login_never_opens_a_browser(spotify_oauth: mock.Mock):
    cache = spotipy.MemoryCacheHandler()
    SpotifyLogin(SPOTIFY_CONFIG, cache)
    assert spotify_oauth.call_args.kwargs["open_browser"] is False
    assert spotify_oauth.call_args.kwargs["cache_handler"] is cache
    assert spotify_oauth.call_args.kwargs["redirect_uri"] == "https://app.example/callback"


def test_spotify_login_authorize_url_includes_state(spotify_oauth: mock.Mock):
    spotify_oauth.return_value.get_authorize_url.return_value = "https://accounts.spotify.com/authorize?state=xyz"
    login = SpotifyLogin(SPOTIFY_CONFIG, spotipy.MemoryCacheHandler())
    assert login.authorize_url("xyz") == "https://accounts.spotify.com/authorize?state=xyz"
    spotify_oauth.return_value.get_authorize_url.assert_called_once_with(state="xyz")


def test_spotify_login_complete_exchanges_code(spotify_oauth: mock.Mock):
    login = SpotifyLogin(SPOTIFY_CONFIG, spotipy.MemoryCacheHandler())
    login.complete("the-code")
    spotify_oauth.return_value.get_access_token.assert_called_once_with(code="the-code", as_dict=False, check_cache=False)


def test_spotify_login_complete_failure(spotify_oauth: mock.Mock):
    spotify_oauth.return_value.get_access_token.side_effect = spotipy.SpotifyOauthError("invalid_grant")
    with pytest.raises(SpotifyAuthError):
        SpotifyLogin(SPOTIFY_CONFIG, spotipy.MemoryCacheHandler()).complete("bad-code")


@pytest.mark.parametrize("cached, expected_session", [
    ({"access_token": "a"}, True),
    (None, False),
    (spotipy.SpotifyOauthError("refresh token revoked"), False),
])
def test_spotify_login_resume(spotify_oauth: mock.Mock, cached: dict[str, str] | None | Exception, expected_session: bool):
    if isinstance(cached, Exception):
        spotify_oauth.return_value.get_cached_token.side_effect = cached
    else:
        spotify_oauth.return_value.get_cached_token.return_value = cached
    session = SpotifyLogin(SPOTIFY_CONFIG, spotipy.MemoryCacheHandler()).resume()
    assert (session is not None) is expected_session
