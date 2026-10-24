# tests/unit/test_auth.py

import pytest
import spotipy
from pytest_mock import MockerFixture
from unittest import mock
from spotify_to_tidal.auth import open_spotify_session, open_tidal_session, SPOTIFY_SCOPES
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
    assert reporter.infos == ["Login with the webbrowser: link.tidal.com/ABCDE"]


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
