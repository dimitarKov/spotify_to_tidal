#!/usr/bin/env python3

import spotipy
import webbrowser
from tidalapi.session import Config, Session

from .errors import SpotifyAuthError, TidalAuthError
from .reporting import get_reporter
from .storage import TidalSessionStore, TidalTokens
from .type import SpotifyConfig

__all__ = [
    'open_spotify_session',
    'open_tidal_session'
]

SPOTIFY_SCOPES = 'playlist-read-private, user-library-read'

def open_spotify_session(config: SpotifyConfig, cache_handler: spotipy.CacheHandler | None = None) -> spotipy.Spotify:
    """ Logs in to Spotify. The token is kept in `cache_handler`; by default spotipy's file cache in the current directory """
    credentials_manager = spotipy.SpotifyOAuth(username=config['username'],
       scope=SPOTIFY_SCOPES,
       client_id=config['client_id'],
       client_secret=config['client_secret'],
       redirect_uri=config['redirect_uri'],
       requests_timeout=10,
       open_browser=config.get('open_browser', True),
       cache_handler=cache_handler)
    try:
        credentials_manager.get_access_token(as_dict=False)
    except spotipy.SpotifyOauthError as e:
        raise SpotifyAuthError("Error opening Spotify session; could not get token for username: {}".format(config['username'])) from e

    return spotipy.Spotify(oauth_manager=credentials_manager)

def open_tidal_session(session_store: TidalSessionStore, config: Config | None = None) -> Session:
    """ Logs in to Tidal, reusing the tokens in `session_store` if they're still valid, and saves the new ones """
    previous_session = session_store.load()

    if config:
        session = Session(config=config)
    else:
        session = Session()
    if previous_session:
        try:
            if session.load_oauth_session(token_type= previous_session['token_type'],
                                   access_token=previous_session['access_token'],
                                   refresh_token=previous_session['refresh_token'] ):
                return session
        except Exception as e:
            get_reporter().warning("Error loading previous Tidal Session: \n" + str(e) )

    login, future = session.login_oauth()
    get_reporter().info('Login with the webbrowser: ' + login.verification_uri_complete)
    url = login.verification_uri_complete
    if not url.startswith('https://'):
        url = 'https://' + url
    webbrowser.open(url)
    try:
        future.result()
    except Exception as e:
        raise TidalAuthError("Tidal login failed or timed out") from e
    if session.token_type is None or session.access_token is None:
        raise TidalAuthError("Tidal login didn't return an access token")
    tokens: TidalTokens = {'session_id': session.session_id,
                           'token_type': session.token_type,
                           'access_token': session.access_token,
                           'refresh_token': session.refresh_token}
    session_store.save(tokens)
    return session


