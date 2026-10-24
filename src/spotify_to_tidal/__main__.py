import yaml
import argparse
import sys

from . import sync as _sync
from . import auth as _auth
from .context import SyncContext
from .errors import SyncError
from .storage import FileTidalSessionStore
from .type import SyncConfig

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config.yml', help='location of the config file')
    parser.add_argument('--uri', help='synchronize a specific URI instead of the one in the config')
    parser.add_argument('--sync-favorites', action=argparse.BooleanOptionalAction, help='synchronize the favorites')
    args = parser.parse_args()
    try:
        run(args)
    except SyncError as e:
        message = f"Error: {e}"
        if e.__cause__ is not None:
            message += f"\n  caused by: {e.__cause__!r}"
        sys.exit(message)

def run(args: argparse.Namespace):
    with open(args.config, 'r') as f:
        config: SyncConfig = yaml.safe_load(f)
    # the CLI keeps its state in files in the current directory
    context = SyncContext.local_files(config)
    print("Opening Spotify session")
    spotify_session = _auth.open_spotify_session(config['spotify'])
    print("Opening Tidal session")
    tidal_session = _auth.open_tidal_session(FileTidalSessionStore())
    if not tidal_session.check_login():
        sys.exit("Could not connect to Tidal")
    if args.uri:
        # if a playlist ID is explicitly provided as a command line argument then use that
        spotify_playlist = spotify_session.playlist(args.uri)
        tidal_playlists = _sync.get_tidal_playlists_wrapper(tidal_session)
        tidal_playlist = _sync.pick_tidal_playlist_for_spotify_playlist(context.playlist_mappings, spotify_playlist, tidal_playlists)
        _sync.sync_playlists_wrapper(context, spotify_session, tidal_session, [tidal_playlist])
        sync_favorites = args.sync_favorites # only sync favorites if command line argument explicitly passed
    elif args.sync_favorites:
        sync_favorites = True # sync only the favorites
    elif config.get('sync_playlists', None):
        # if the config contains a sync_playlists list of mappings then use that
        _sync.sync_playlists_wrapper(context, spotify_session, tidal_session, _sync.get_playlists_from_config(spotify_session, tidal_session, config))
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)
    else:
        # otherwise sync all the user playlists in the Spotify account and favorites unless explicitly disabled
        _sync.sync_playlists_wrapper(context, spotify_session, tidal_session, _sync.get_user_playlist_mappings(context, spotify_session, tidal_session))
        sync_favorites = args.sync_favorites is None and config.get('sync_favorites_default', True)

    if sync_favorites:
        _sync.sync_favorites_wrapper(context, spotify_session, tidal_session)

if __name__ == '__main__':
    main()
    sys.exit(0)
