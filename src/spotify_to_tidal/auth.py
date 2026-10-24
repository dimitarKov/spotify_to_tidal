#!/usr/bin/env python3

""" Logging in to Spotify and Tidal.

    Each service has a step-by-step flow a web app can drive (SpotifyLogin; resume_tidal_session /
    start_tidal_login), and an interactive function for the CLI built on it (open_spotify_session,
    open_tidal_session) """

import concurrent.futures
import spotipy
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable
from tidalapi.session import Config, LinkLogin, Session

from .errors import SpotifyAuthError, TidalAuthError
from .reporting import get_reporter
from .storage import TidalSessionStore, TidalTokens
from .type import SpotifyConfig

__all__ = [
    'SpotifyLogin',
    'open_spotify_session',
    'TidalLoginLink',
    'PendingTidalLogin',
    'resume_tidal_session',
    'start_tidal_login',
    'open_tidal_session',
]

SPOTIFY_SCOPES = 'playlist-read-private, user-library-read'

def _spotify_oauth(config: SpotifyConfig, cache_handler: spotipy.CacheHandler | None, open_browser: bool) -> spotipy.SpotifyOAuth:
    return spotipy.SpotifyOAuth(username=config['username'],
       scope=SPOTIFY_SCOPES,
       client_id=config['client_id'],
       client_secret=config['client_secret'],
       redirect_uri=config['redirect_uri'],
       requests_timeout=10,
       open_browser=open_browser,
       cache_handler=cache_handler)


class SpotifyLogin:
    """ Spotify's OAuth authorization code flow, driven by the caller (e.g. a web app):
        1. send the user to authorize_url(state), with a random `state` remembered for this user;
           Spotify redirects them back to config['redirect_uri'] with ?code=...&state=...
        2. check the returned state matches (it protects against forged callbacks), then call complete(code)
        The token is kept in `cache_handler`, so later resume() logs in without the user while it can be refreshed """

    def __init__(self, config: SpotifyConfig, cache_handler: spotipy.CacheHandler):
        self._oauth = _spotify_oauth(config, cache_handler, open_browser=False)

    def authorize_url(self, state: str) -> str:
        return self._oauth.get_authorize_url(state=state)

    def complete(self, code: str) -> spotipy.Spotify:
        """ Exchanges the code from Spotify's redirect for a token, and stores it """
        try:
            self._oauth.get_access_token(code=code, as_dict=False, check_cache=False)
        except spotipy.SpotifyOauthError as e:
            raise SpotifyAuthError("Spotify login failed") from e
        return spotipy.Spotify(oauth_manager=self._oauth)

    def resume(self) -> spotipy.Spotify | None:
        """ A session from the stored token, refreshing it if expired; None if the user needs to log in """
        try:
            token = self._oauth.get_cached_token()
        except spotipy.SpotifyOauthError:
            return None  # e.g. the refresh token was revoked
        return spotipy.Spotify(oauth_manager=self._oauth) if token else None


def open_spotify_session(config: SpotifyConfig, cache_handler: spotipy.CacheHandler | None = None) -> spotipy.Spotify:
    """ Logs in to Spotify interactively (opens the browser and asks for the redirect URL in the terminal).
        The token is kept in `cache_handler`; by default spotipy's file cache in the current directory """
    credentials_manager = _spotify_oauth(config, cache_handler, open_browser=config.get('open_browser', True))
    try:
        credentials_manager.get_access_token(as_dict=False)
    except spotipy.SpotifyOauthError as e:
        raise SpotifyAuthError("Error opening Spotify session; could not get token for username: {}".format(config['username'])) from e

    return spotipy.Spotify(oauth_manager=credentials_manager)


def _new_tidal_session(config: Config | None) -> Session:
    return Session(config=config) if config else Session()

def resume_tidal_session(session_store: TidalSessionStore, config: Config | None = None) -> Session | None:
    """ A session from the tokens in `session_store`, if they're still valid; None if the user needs to log in """
    previous_session = session_store.load()
    if not previous_session:
        return None
    session = _new_tidal_session(config)
    try:
        if session.load_oauth_session(token_type= previous_session['token_type'],
                               access_token=previous_session['access_token'],
                               refresh_token=previous_session['refresh_token'] ):
            return session
    except Exception as e:
        get_reporter().warning("Error loading previous Tidal Session: \n" + str(e) )
    return None


@dataclass(frozen=True)
class TidalLoginLink:
    """ What to show the user: open `url` (the code is already included) or enter `user_code` at link.tidal.com """
    url: str
    user_code: str
    expires_in: float  # seconds


class PendingTidalLogin:
    """ A Tidal device login waiting for the user to approve it at `link`. Poll done(), then call result() """

    def __init__(self, session: Session, link_login: LinkLogin, future: concurrent.futures.Future[Any], session_store: TidalSessionStore):
        self._session = session
        self._future = future
        self._session_store = session_store
        url = link_login.verification_uri_complete
        if not url.startswith('https://'):
            url = 'https://' + url
        self.link = TidalLoginLink(url=url, user_code=link_login.user_code, expires_in=link_login.expires_in)

    def done(self) -> bool:
        """ True once the user has logged in, or the login failed or expired """
        return self._future.done()

    def result(self) -> Session:
        """ Waits for the login to finish, saves the tokens and returns the session """
        try:
            self._future.result()
        except Exception as e:
            raise TidalAuthError("Tidal login failed or timed out") from e
        session = self._session
        if session.token_type is None or session.access_token is None:
            raise TidalAuthError("Tidal login didn't return an access token")
        tokens: TidalTokens = {'session_id': session.session_id,
                               'token_type': session.token_type,
                               'access_token': session.access_token,
                               'refresh_token': session.refresh_token}
        self._session_store.save(tokens)
        return session


def start_tidal_login(session_store: TidalSessionStore, config: Config | None = None) -> PendingTidalLogin:
    """ Starts a Tidal device login; the tokens are saved to `session_store` once it completes """
    session = _new_tidal_session(config)
    link_login, future = session.login_oauth()
    return PendingTidalLogin(session, link_login, future, session_store)


def open_tidal_session(session_store: TidalSessionStore, config: Config | None = None,
                       open_browser: Callable[[str], object] | None = None) -> Session:
    """ Logs in to Tidal interactively, reusing the tokens in `session_store` if they're still valid.
        Otherwise shows the login link and opens it with `open_browser` (default: the system browser) """
    session = resume_tidal_session(session_store, config)
    if session:
        return session
    pending = start_tidal_login(session_store, config)
    get_reporter().info('Login with the webbrowser: ' + pending.link.url)
    (open_browser or webbrowser.open)(pending.link.url)
    return pending.result()
