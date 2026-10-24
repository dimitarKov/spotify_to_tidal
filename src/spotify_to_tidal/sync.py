#!/usr/bin/env python3

import asyncio
from .cache import TrackMatchCache
from .context import SyncContext
from .storage import MatchFailureStore
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, List, Mapping, Optional, ParamSpec, Sequence, Set, Tuple, TypeVar, cast
import math
import re
import requests
import spotipy
from tidalapi.album import Album
from tidalapi.exceptions import InvalidISRC, ObjectNotFound, TooManyRequests
from tidalapi.media import Track
from tidalapi.playlist import Playlist, UserPlaylist
from tidalapi.session import Session
from tidalapi.user import LoggedInUser
from .tidalapi_patch import add_multiple_tracks_to_playlist, clear_tidal_playlist, get_all_favorites, get_all_playlists, get_all_playlist_tracks
from .progress import gather_with_progress
from .rate_limiter import RateLimiter
from .errors import PlaylistNotEditableError, PlaylistNotFoundError, RetriesExhaustedError, TidalAuthError
from .reporting import get_reporter
import unicodedata

from .type import SpotifyAlbum, SpotifyArtist, SpotifyPage, SpotifyPlaylist, SpotifyTrack, SyncConfig

P = ParamSpec('P')
T = TypeVar('T')

PlaylistMapping = Tuple[SpotifyPlaylist, Optional[Playlist]]

def normalize(s: str) -> str:
    return unicodedata.normalize('NFD', s).encode('ascii', 'ignore').decode('ascii')

def simple(input_string: str) -> str:
    # only take the first part of a string before any hyphens or brackets to account for different versions
    return input_string.split('-')[0].strip().split('(')[0].strip().split('[')[0].strip()

def simple_artist(artist_name: str) -> str:
    """ Normalises an artist name for comparison. Unlike in titles, hyphens are part of artist names (Jay-Z, Ken-Y),
        so keep the whole name and only treat hyphens as spaces, so 'JAY-Z' and 'Jay Z' compare equal.
        Bracketed suffixes are still dropped, as for titles """
    name = artist_name.split('(')[0].split('[')[0]
    return ' '.join(name.lower().replace('-', ' ').split())

def title_key(title: str, simplify: bool = True) -> str:
    """ Normalises a track title for comparison: drops version suffixes ('- Remastered', '(Live)') and 'feat.',
        accents, punctuation, case and extra whitespace. Letters in any script are kept """
    title = title.lower()
    if simplify:
        title = simple(title).split('feat.')[0]
    without_accents = ''.join(c for c in unicodedata.normalize('NFD', title) if unicodedata.category(c) != 'Mn')
    return ' '.join(re.sub(r'[^\w\s]', '', without_accents).split())

def isrc_match(tidal_track: Track, spotify_track: SpotifyTrack) -> bool:
    external_ids = spotify_track.get("external_ids", {})
    if "isrc" in external_ids:
        return tidal_track.isrc == external_ids["isrc"]
    return False

def duration_match(tidal_track: Track, spotify_track: SpotifyTrack, tolerance: float=2) -> bool:
    # the duration of the two tracks must be the same to within 2 seconds
    return abs(tidal_track.duration - spotify_track['duration_ms']/1000) < tolerance

def name_match(tidal_track: Track, spotify_track: SpotifyTrack) -> bool:
    def exclusion_rule(pattern: str, tidal_track: Track, spotify_track: SpotifyTrack):
        spotify_has_pattern = pattern in spotify_track['name'].lower()
        tidal_has_pattern = pattern in tidal_track.name.lower() or (not tidal_track.version is None and (pattern in tidal_track.version.lower()))
        return spotify_has_pattern != tidal_has_pattern

    # handle some edge cases
    if exclusion_rule("instrumental", tidal_track, spotify_track): return False
    if exclusion_rule("acapella", tidal_track, spotify_track): return False
    if exclusion_rule("remix", tidal_track, spotify_track): return False

    # the simplified titles must be equal, e.g. 'Song - 2011 Remaster' == 'Song (Remastered)' but not 'Different Song'
    spotify_key, tidal_key = title_key(spotify_track['name']), title_key(tidal_track.name)
    if not spotify_key:
        # nothing left after simplifying, e.g. '(Untitled)': compare the whole titles instead
        spotify_key, tidal_key = title_key(spotify_track['name'], simplify=False), title_key(tidal_track.name, simplify=False)
    if not spotify_key:
        # only punctuation, e.g. '?'
        return spotify_track['name'].strip() == tidal_track.name.strip()
    return spotify_key == tidal_key

def artist_match(tidal: Track | Album, spotify: SpotifyTrack | SpotifyAlbum) -> bool:
    def split_artist_name(artist: str) -> Sequence[str]:
       if '&' in artist:
           return artist.split('&')
       elif ',' in artist:
           return artist.split(',')
       else:
           return [artist]

    def get_artists(names: Sequence[Optional[str]], do_normalize: bool) -> Set[str]:
        result: list[str] = []
        for artist_name in names:
            if artist_name is None:
                continue
            if do_normalize:
                artist_name = normalize(artist_name)
            result.extend(split_artist_name(artist_name))
        return set([simple_artist(x) for x in result])

    def get_tidal_artists(tidal: Track | Album, do_normalize: bool=False) -> Set[str]:
        return get_artists([artist.name for artist in tidal.artists or []], do_normalize)

    def get_spotify_artists(spotify: SpotifyTrack | SpotifyAlbum, do_normalize: bool=False) -> Set[str]:
        artists: List[SpotifyArtist] = spotify['artists']
        return get_artists([artist['name'] for artist in artists], do_normalize)
    # There must be at least one overlapping artist between the Tidal and Spotify track
    # Try with both un-normalized and then normalized
    if get_tidal_artists(tidal).intersection(get_spotify_artists(spotify)) != set():
        return True
    return get_tidal_artists(tidal, True).intersection(get_spotify_artists(spotify, True)) != set()

def match(tidal_track: Track, spotify_track: SpotifyTrack) -> bool:
    if not spotify_track['id']: return False
    return isrc_match(tidal_track, spotify_track) or (
        duration_match(tidal_track, spotify_track)
        and name_match(tidal_track, spotify_track)
        and artist_match(tidal_track, spotify_track)
    )

def test_album_similarity(spotify_album: SpotifyAlbum, tidal_album: Album, threshold: float=0.6) -> bool:
    return SequenceMatcher(None, simple(spotify_album['name']), simple(tidal_album.name or '')).ratio() >= threshold and artist_match(tidal_album, spotify_album)

class IsrcLookup:
    """ Looks tracks up on Tidal by ISRC, which identifies a recording exactly.
        Best effort: tidalapi reports every HTTP error from this endpoint as InvalidISRC, including the endpoint
        being unavailable to our session, so after repeated consecutive errors stop trying for the rest of the run
        rather than wasting a request per track """

    MAX_CONSECUTIVE_ERRORS = 5

    def __init__(self, tidal_session: Session):
        self._tidal_session = tidal_session
        self._consecutive_errors = 0

    @property
    def enabled(self) -> bool:
        return self._consecutive_errors < self.MAX_CONSECUTIVE_ERRORS

    async def find(self, isrc: str) -> List[Track]:
        if not self.enabled:
            return []
        try:
            tracks = await asyncio.to_thread(self._tidal_session.get_tracks_by_isrc, isrc)
        except ObjectNotFound: # no track with this ISRC
            self._consecutive_errors = 0
            return []
        except InvalidISRC:
            self._consecutive_errors += 1
            if not self.enabled:
                get_reporter().warning(f"Tidal ISRC lookup failed {self._consecutive_errors} times in a row, falling back to text search only")
            return []
        self._consecutive_errors = 0
        return tracks

async def tidal_search(spotify_track: SpotifyTrack, rate_limiter: RateLimiter, tidal_session: Session, failure_cache: MatchFailureStore,
                       isrc_lookup: IsrcLookup | None = None,
                       candidates: List[Track] | None = None) -> Track | None:
    """ Searches Tidal for the Spotify track. Every Tidal track considered is appended to `candidates`, if given,
        so the closest one can be reported when nothing matches """
    spotify_id = spotify_track['id']
    if spotify_id is None:
        return None

    def _consider(track: Track):
        if candidates is not None:
            candidates.append(track)

    async def _search_by_isrc() -> Track | None:
        isrc = spotify_track.get('external_ids', {}).get('isrc')
        if isrc_lookup is None or not isrc_lookup.enabled or not isrc:
            return None
        await rate_limiter.acquire()
        for track in await isrc_lookup.find(isrc):
            _consider(track)
            if track.available and match(track, spotify_track):
                failure_cache.remove_match_failure(spotify_id)
                return track

    def _search_for_track_in_album() -> Track | None:
        # search for album name and first album artist
        album_artist = spotify_track['album']['artists'][0]['name'] if spotify_track['album']['artists'] else None
        if album_artist is not None:
            query = simple(spotify_track['album']['name']) + " " + album_artist.strip()
            album_result = tidal_session.search(query, models=[Album])
            for album in album_result['albums']:
                if album.num_tracks is not None and album.num_tracks >= spotify_track['track_number'] and test_album_similarity(spotify_track['album'], album):
                    try:
                        album_tracks = album.tracks()
                    except ObjectNotFound:
                        continue # album listed in search results but not available, e.g. in the user's region
                    if len(album_tracks) < spotify_track['track_number']:
                        continue # incorrect metadata :(
                    track = album_tracks[spotify_track['track_number'] - 1]
                    _consider(track)
                    if match(track, spotify_track):
                        failure_cache.remove_match_failure(spotify_id)
                        return track

    def _search_for_standalone_track() -> Track | None:
        # if album search fails then search for track name and first artist
        query = simple(spotify_track['name']) + ' ' + (spotify_track['artists'][0]['name'] or '').strip()
        for track in tidal_session.search(query, models=[Track])['tracks']:
            _consider(track)
            if match(track, spotify_track):
                failure_cache.remove_match_failure(spotify_id)
                return track
    isrc_search = await _search_by_isrc()
    if isrc_search:
        return isrc_search
    await rate_limiter.acquire()
    album_search = await asyncio.to_thread( _search_for_track_in_album )
    if album_search:
        return album_search
    await rate_limiter.acquire()
    track_search = await asyncio.to_thread( _search_for_standalone_track )
    if track_search:
        return track_search

    # if none of the search modes succeeded then store the track id to the failure cache
    failure_cache.cache_match_failure(spotify_id)

def explain_mismatch(tidal_track: Track, spotify_track: SpotifyTrack) -> List[str]:
    """ The reasons match() rejects this Tidal track for the Spotify track """
    reasons: List[str] = []
    if not tidal_track.available:
        reasons.append("not available")
    isrc = spotify_track.get('external_ids', {}).get('isrc')
    if isrc and tidal_track.isrc and tidal_track.isrc != isrc:
        reasons.append("different ISRC")
    if not name_match(tidal_track, spotify_track):
        reasons.append("title differs")
    if not artist_match(tidal_track, spotify_track):
        reasons.append("artist differs")
    if not duration_match(tidal_track, spotify_track):
        reasons.append(f"duration differs by {abs(tidal_track.duration - spotify_track['duration_ms']/1000):.0f}s")
    return reasons

def closest_candidate(candidates: Sequence[Track], spotify_track: SpotifyTrack) -> Track | None:
    """ The candidate failing the fewest of the title/artist/duration checks, then closest in duration """
    def _score(track: Track) -> Tuple[int, float]:
        failed = [not name_match(track, spotify_track), not artist_match(track, spotify_track), not duration_match(track, spotify_track)]
        return sum(failed), abs(track.duration - spotify_track['duration_ms']/1000)
    return min(candidates, key=_score, default=None)

def describe_no_match(spotify_track: SpotifyTrack, candidates: Sequence[Track]) -> str:
    """ One line for "songs not found.txt" explaining why the closest Tidal candidate was rejected """
    closest = closest_candidate(candidates, spotify_track)
    if closest is None:
        return "no candidates found on Tidal"
    artists = ', '.join(artist.name or '' for artist in closest.artists or [])
    version = f" ({closest.version})" if closest.version else ""
    reasons = ', '.join(explain_mismatch(closest, spotify_track)) or "rejected"
    return f"closest on Tidal: {artists} - {closest.name}{version} [{closest.id}]: {reasons}"

MAX_RETRY_AFTER = 10*60 # if a server asks us to wait longer than this, give up instead

def _is_transient_error(e: Exception) -> bool:
    """ True for errors worth retrying: rate limits, server errors and network failures """
    if isinstance(e, TooManyRequests):
        return True
    if isinstance(e, spotipy.exceptions.SpotifyException):
        # QUOTA_EXCEEDED is a 429 that won't clear up by retrying soon
        return (e.http_status == 429 and e.reason != 'QUOTA_EXCEEDED') or e.http_status >= 500
    if isinstance(e, requests.exceptions.HTTPError):
        return e.response is not None and (e.response.status_code == 429 or e.response.status_code >= 500)
    # connection errors, timeouts etc.
    return isinstance(e, requests.exceptions.RequestException)

def _get_retry_after(e: Exception) -> int | None:
    """ Seconds the server asked us to wait before retrying, if it said """
    if isinstance(e, TooManyRequests):
        return e.retry_after if e.retry_after >= 0 else None
    headers: Mapping[str, Any]
    if isinstance(e, spotipy.exceptions.SpotifyException):
        headers = e.headers
    elif isinstance(e, requests.exceptions.RequestException) and e.response is not None:
        headers = e.response.headers
    else:
        return None
    retry_after = headers.get('Retry-After')
    if retry_after is None:
        return None
    try:
        return int(retry_after)
    except (TypeError, ValueError):
        return None

async def repeat_on_request_error(function: Callable[P, Awaitable[T]], *args: P.args, **kwargs: P.kwargs) -> T:
    # utility to repeat calling the function up to 5 times if a transient error is thrown
    # other errors (e.g. 403/404) are raised immediately for the caller to handle
    remaining = 5
    while True:
        try:
            return await function(*args, **kwargs)
        except (TooManyRequests, requests.exceptions.RequestException, spotipy.exceptions.SpotifyException) as e:
            if not _is_transient_error(e):
                raise
            retry_after = _get_retry_after(e)
            error = str(e)
            if isinstance(e, requests.exceptions.RequestException) and not e.response is None and e.response.text:
                error += f" (response: {e.response.text[:200]})"
            if retry_after is not None and retry_after > MAX_RETRY_AFTER:
                raise RetriesExhaustedError(f"{error}; the server asked to wait {retry_after}s, giving up") from e
            if not remaining:
                raise RetriesExhaustedError(f"{error}; still failing after retrying, giving up") from e
            get_reporter().warning(f"{error} occurred, retrying {remaining} times")
            sleep_schedule = {5: 1, 4:10, 3:60, 2:5*60, 1:10*60} # sleep variable length of time depending on retry number
            await asyncio.sleep(retry_after if retry_after is not None else sleep_schedule.get(remaining, 1))
            remaining -= 1


def _extract_track(entry: Mapping[str, Any]) -> SpotifyTrack | None:
    """ Gets the track object from a Spotify playlist or saved-tracks entry.
        Playlist entries hold it in 'item' (where 'track' may now be a boolean), saved tracks still use 'track' """
    track = entry.get('item')
    if not isinstance(track, dict):
        track = entry.get('track')
    if not isinstance(track, dict):
        return None
    return cast(SpotifyTrack, track)  # untyped API JSON, validated by the callers

async def _fetch_all_from_spotify_in_chunks(fetch_function: Callable[[int], SpotifyPage]) -> List[SpotifyTrack]:
    def _extract_tracks(results: SpotifyPage) -> List[SpotifyTrack]:
        return [track for track in map(_extract_track, results['items']) if track is not None]

    output: List[SpotifyTrack] = []
    results = fetch_function(0)
    output.extend(_extract_tracks(results))

    # Get all the remaining tracks in parallel
    if results['next']:
        offsets = [results['limit'] * n for n in range(1, math.ceil(results['total'] / results['limit']))]
        extra_results = await gather_with_progress(
            [asyncio.to_thread(fetch_function, offset) for offset in offsets],
            desc="Fetching additional data chunks"
        )
        for extra_result in extra_results:
            output.extend(_extract_tracks(extra_result))

    return output


async def get_tracks_from_spotify_playlist(spotify_session: spotipy.Spotify, spotify_playlist: SpotifyPlaylist) -> List[SpotifyTrack]:
    def _get_tracks_from_spotify_playlist(offset: int) -> SpotifyPage:
        fields = "next,total,limit,items(item(name,album(name,artists),artists,track_number,duration_ms,id,external_ids(isrc),type))"
        return spotify_session.playlist_items(playlist_id=spotify_playlist["id"], fields=fields, offset=offset, additional_types=('track',))

    def track_filter(item: SpotifyTrack) -> bool:
        return item.get('type', 'track') == 'track' # type may be 'episode' also

    def sanity_filter(item: Mapping[str, Any]) -> bool:
        # checked on the raw JSON since Spotify may omit any of these
        return ('album' in item
                and 'name' in item['album']
                and 'artists' in item['album']
                and len(item['album']['artists']) > 0
                and item['album']['artists'][0]['name'] is not None)

    get_reporter().info(f"Loading tracks from Spotify playlist '{spotify_playlist['name']}'")
    items = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, _get_tracks_from_spotify_playlist)
    return list(filter(sanity_filter, filter(track_filter, items)))

def populate_track_match_cache(track_match_cache: TrackMatchCache, spotify_tracks_: Sequence[SpotifyTrack], tidal_tracks_: Sequence[Track]):
    """ Populate the track match cache with all the existing tracks in Tidal playlist corresponding to Spotify playlist """
    def _populate_one_track_from_spotify(spotify_track: SpotifyTrack):
        spotify_id = spotify_track['id']
        if spotify_id is None: return
        for idx, tidal_track in list(enumerate(tidal_tracks)):
            if tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_id, tidal_track.id))
                tidal_tracks.pop(idx)
                return

    def _populate_one_track_from_tidal(tidal_track: Track):
        for idx, spotify_track in list(enumerate(spotify_tracks)):
            spotify_id = spotify_track['id']
            if spotify_id is not None and tidal_track.available and match(tidal_track, spotify_track):
                track_match_cache.insert((spotify_id, tidal_track.id))
                spotify_tracks.pop(idx)
                return

    # make a copy of the tracks to avoid modifying original arrays
    spotify_tracks = [t for t in spotify_tracks_]
    tidal_tracks = [t for t in tidal_tracks_]

    # first populate from the tidal tracks
    for track in tidal_tracks:
        _populate_one_track_from_tidal(track)
    # then populate from the subset of Spotify tracks that didn't match (to account for many-to-one style mappings)
    for track in spotify_tracks:
        _populate_one_track_from_spotify(track)

def get_new_spotify_tracks(context: SyncContext, spotify_tracks: Sequence[SpotifyTrack]) -> List[SpotifyTrack]:
    ''' Extracts only the tracks that have not already been seen in our Tidal caches '''
    results: List[SpotifyTrack] = []
    for spotify_track in spotify_tracks:
        spotify_id = spotify_track['id']
        if not spotify_id: continue
        if not context.track_match_cache.get(spotify_id) and not context.failure_cache.has_match_failure(spotify_id):
            results.append(spotify_track)
    return results

def _artist_names(spotify_track: SpotifyTrack, separator: str) -> str:
    return separator.join([artist['name'] or '' for artist in spotify_track['artists']])

def get_tracks_for_new_tidal_playlist(track_match_cache: TrackMatchCache, spotify_tracks: Sequence[SpotifyTrack]) -> List[int]:
    ''' gets list of corresponding tidal track ids for each spotify track, ignoring duplicates '''
    output: List[int] = []
    seen_tracks: Set[int] = set()

    for spotify_track in spotify_tracks:
        spotify_id = spotify_track['id']
        if not spotify_id: continue
        tidal_id = track_match_cache.get(spotify_id)
        if tidal_id:
            if tidal_id in seen_tracks:
                track_name = spotify_track['name']
                artist_names = _artist_names(spotify_track, ', ')
                get_reporter().warning(f'Duplicate found: Track "{track_name}" by {artist_names} will be ignored')
            else:
                output.append(tidal_id)
                seen_tracks.add(tidal_id)
    return output

async def search_new_tracks_on_tidal(context: SyncContext, tidal_session: Session, spotify_tracks: Sequence[SpotifyTrack], playlist_name: str):
    """ Generic function for searching for each item in a list of Spotify tracks which have not already been seen and adding them to the cache """
    # Extract the new tracks that do not already exist in the old tidal tracklist
    tracks_to_search = get_new_spotify_tracks(context, spotify_tracks)
    if not tracks_to_search:
        return

    # Search for each of the tracks on Tidal concurrently
    task_description = "Searching Tidal for {}/{} tracks in Spotify playlist '{}'".format(len(tracks_to_search), len(spotify_tracks), playlist_name)
    config = context.config
    rate_limiter = RateLimiter(rate=config.get('rate_limit', 10), capacity=config.get('max_concurrency', 10))
    isrc_lookup = IsrcLookup(tidal_session)
    candidates: List[List[Track]] = [[] for _ in tracks_to_search]
    search_results = await gather_with_progress([ repeat_on_request_error(tidal_search, t, rate_limiter, tidal_session, context.failure_cache, isrc_lookup, c)
                                                  for t, c in zip(tracks_to_search, candidates) ], desc=task_description )

    # Add the search results to the cache
    song404: List[str] = []
    for spotify_track, search_result, track_candidates in zip(tracks_to_search, search_results, candidates):
        spotify_id = spotify_track['id']
        if search_result and spotify_id is not None:
            context.track_match_cache.insert( (spotify_id, search_result.id) )
        else:
            song = f"{spotify_id}: {_artist_names(spotify_track, ',')} - {spotify_track['name']}"
            get_reporter().warning("Could not find the track " + song)
            song404.append(f"{song}\n    {describe_no_match(spotify_track, track_candidates)}")
    context.not_found.record(playlist_name, song404)

def _get_logged_in_user(tidal_session: Session) -> LoggedInUser:
    if not isinstance(tidal_session.user, LoggedInUser):
        raise TidalAuthError("Not logged in to Tidal")
    return tidal_session.user

def _as_user_playlist(playlist: Playlist) -> UserPlaylist:
    """ Only playlists owned by the user can be modified """
    if not isinstance(playlist, UserPlaylist):
        raise PlaylistNotEditableError(f"Tidal playlist '{playlist.name}' ({playlist.id}) is not owned by the user and can't be modified")
    return playlist

async def sync_playlist(context: SyncContext, spotify_session: spotipy.Spotify, tidal_session: Session, spotify_playlist: SpotifyPlaylist, tidal_playlist: Playlist | None):
    """ sync given playlist to tidal """
    # Get the tracks from both Spotify and Tidal, creating a new Tidal playlist if necessary
    spotify_tracks = await get_tracks_from_spotify_playlist(spotify_session, spotify_playlist)
    if len(spotify_tracks) == 0:
        return # nothing to do
    old_tidal_tracks: List[Track]
    if tidal_playlist:
        user_playlist = _as_user_playlist(tidal_playlist)
        old_tidal_tracks = await get_all_playlist_tracks(user_playlist)
    else:
        get_reporter().info(f"No playlist found on Tidal corresponding to Spotify playlist: '{spotify_playlist['name']}', creating new playlist")
        user_playlist = _get_logged_in_user(tidal_session).create_playlist(spotify_playlist['name'], spotify_playlist['description'] or '')
        old_tidal_tracks = []

    # Extract the new tracks from the playlist that we haven't already seen before
    populate_track_match_cache(context.track_match_cache, spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(context, tidal_session, spotify_tracks, spotify_playlist['name'])
    new_tidal_track_ids = get_tracks_for_new_tidal_playlist(context.track_match_cache, spotify_tracks)

    # Update the Tidal playlist if there are changes
    old_tidal_track_ids = [t.id for t in old_tidal_tracks]
    if new_tidal_track_ids == old_tidal_track_ids:
        get_reporter().info("No changes to write to Tidal playlist")
    elif new_tidal_track_ids[:len(old_tidal_track_ids)] == old_tidal_track_ids:
        # Append new tracks to the existing playlist if possible
        add_multiple_tracks_to_playlist(user_playlist, new_tidal_track_ids[len(old_tidal_track_ids):])
    else:
        # Erase old playlist and add new tracks from scratch if any reordering occured
        clear_tidal_playlist(user_playlist)
        add_multiple_tracks_to_playlist(user_playlist, new_tidal_track_ids)

async def sync_favorites(context: SyncContext, spotify_session: spotipy.Spotify, tidal_session: Session):
    """ sync user favorites to tidal """
    async def get_tracks_from_spotify_favorites() -> List[SpotifyTrack]:
        def _get_favorite_tracks(offset: int) -> SpotifyPage:
            return spotify_session.current_user_saved_tracks(offset=offset)
        tracks = await repeat_on_request_error( _fetch_all_from_spotify_in_chunks, _get_favorite_tracks)
        tracks.reverse()
        return tracks

    def get_new_tidal_favorites() -> List[int]:
        existing_favorite_ids = set([track.id for track in old_tidal_tracks])
        new_ids: List[int] = []
        for spotify_track in spotify_tracks:
            spotify_id = spotify_track['id']
            match_id = context.track_match_cache.get(spotify_id) if spotify_id is not None else None
            if match_id and not match_id in existing_favorite_ids:
                new_ids.append(match_id)
        return new_ids

    favorites = _get_logged_in_user(tidal_session).favorites
    get_reporter().info("Loading favorite tracks from Spotify")
    spotify_tracks = await get_tracks_from_spotify_favorites()
    get_reporter().info("Loading existing favorite tracks from Tidal")
    old_tidal_tracks = await get_all_favorites(favorites, order='DATE')
    populate_track_match_cache(context.track_match_cache, spotify_tracks, old_tidal_tracks)
    await search_new_tracks_on_tidal(context, tidal_session, spotify_tracks, "Favorites")
    new_tidal_favorite_ids = get_new_tidal_favorites()
    if new_tidal_favorite_ids:
        with get_reporter().progress("Adding new tracks to Tidal favorites", total=len(new_tidal_favorite_ids)) as progress:
            for tidal_id in new_tidal_favorite_ids:
                favorites.add_track(str(tidal_id))
                progress.update(1)
    else:
        get_reporter().info("No new tracks to add to Tidal favorites")

def sync_playlists_wrapper(context: SyncContext, spotify_session: spotipy.Spotify, tidal_session: Session, playlists: Sequence[PlaylistMapping]):
  for spotify_playlist, tidal_playlist in playlists:
    # sync the spotify playlist to tidal
    try:
      asyncio.run(sync_playlist(context, spotify_session, tidal_session, spotify_playlist, tidal_playlist) )
    except spotipy.exceptions.SpotifyException as e:
      if e.http_status not in (403, 404):
        raise
      # Spotify only returns the contents of playlists the user owns or collaborates on
      get_reporter().warning(f"Skipping Spotify playlist '{spotify_playlist['name']}': access denied or not found ({e.http_status})")

def sync_favorites_wrapper(context: SyncContext, spotify_session: spotipy.Spotify, tidal_session: Session):
    asyncio.run(main=sync_favorites(context, spotify_session=spotify_session, tidal_session=tidal_session))

def get_tidal_playlists_wrapper(tidal_session: Session) -> Mapping[str, Playlist]:
    tidal_playlists = asyncio.run(get_all_playlists(_get_logged_in_user(tidal_session)))
    return {playlist.name: playlist for playlist in tidal_playlists if playlist.name is not None}

def pick_tidal_playlist_for_spotify_playlist(spotify_playlist: SpotifyPlaylist, tidal_playlists: Mapping[str, Playlist]) -> PlaylistMapping:
    if spotify_playlist['name'] in tidal_playlists:
      # if there's an existing tidal playlist with the name of the current playlist then use that
      tidal_playlist = tidal_playlists[spotify_playlist['name']]
      return (spotify_playlist, tidal_playlist)
    else:
      return (spotify_playlist, None)

def get_user_playlist_mappings(spotify_session: spotipy.Spotify, tidal_session: Session, config: SyncConfig) -> List[PlaylistMapping]:
    results: List[PlaylistMapping] = []
    spotify_playlists = asyncio.run(get_playlists_from_spotify(spotify_session, config))
    tidal_playlists = get_tidal_playlists_wrapper(tidal_session)
    for spotify_playlist in spotify_playlists:
        results.append( pick_tidal_playlist_for_spotify_playlist(spotify_playlist, tidal_playlists) )
    return results

async def get_playlists_from_spotify(spotify_session: spotipy.Spotify, config: SyncConfig) -> List[SpotifyPlaylist]:
    # get all the playlists from the Spotify account
    def _get_playlists(offset: int) -> SpotifyPage:
        return spotify_session.current_user_playlists(offset=offset)

    playlists: List[Optional[SpotifyPlaylist]] = []
    get_reporter().info("Loading Spotify playlists")
    first_results = _get_playlists(0)
    exclude_list = set([x.split(':')[-1] for x in config.get('excluded_playlists', [])])
    playlists.extend([p for p in first_results['items']])
    user_id: str = spotify_session.current_user()['id']

    # get all the remaining playlists in parallel
    if first_results['next']:
        offsets = [ first_results['limit'] * n for n in range(1, math.ceil(first_results['total']/first_results['limit'])) ]
        extra_results = await gather_with_progress([asyncio.to_thread(_get_playlists, offset) for offset in offsets])
        for extra_result in extra_results:
            playlists.extend([p for p in extra_result['items']])

    # filter out playlists that don't belong to us or are on the exclude list
    return [p for p in playlists if p and p['owner']['id'] == user_id and not p['id'] in exclude_list]

def get_playlists_from_config(spotify_session: spotipy.Spotify, tidal_session: Session, config: SyncConfig) -> List[PlaylistMapping]:
    # get the list of playlist sync mappings from the configuration file
    output: List[PlaylistMapping] = []
    for item in config.get('sync_playlists', []):
        spotify_id, tidal_id = item['spotify_id'], item['tidal_id']
        try:
            spotify_playlist: SpotifyPlaylist = spotify_session.playlist(playlist_id=spotify_id)
        except spotipy.SpotifyException as e:
            raise PlaylistNotFoundError(f"Error getting Spotify playlist {spotify_id}") from e
        try:
            tidal_playlist = tidal_session.playlist(playlist_id=tidal_id)
        except Exception as e:
            raise PlaylistNotFoundError(f"Error getting Tidal playlist {tidal_id}") from e
        output.append((spotify_playlist, tidal_playlist))
    return output
