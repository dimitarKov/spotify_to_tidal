class SyncError(Exception):
    """ An error that stops the sync. The message is meant to be shown to the user; the underlying
        exception, if any, is chained as __cause__ """


class SpotifyAuthError(SyncError):
    """ Couldn't log in to Spotify """


class TidalAuthError(SyncError):
    """ Couldn't log in to Tidal """


class PlaylistNotFoundError(SyncError):
    """ A playlist given in the config or on the command line couldn't be loaded """


class PlaylistNotEditableError(SyncError):
    """ The Tidal playlist isn't owned by the user, so it can't be modified """


class RetriesExhaustedError(SyncError):
    """ A request kept failing with transient errors (rate limits, server or network errors) """
