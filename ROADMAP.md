# Roadmap

Fork goal: a working, reliable sync for me and my friends, then a hosted web UI.
Work through items in order; tick each one off when it is merged to `main`.

Each item gets its own branch (`fix/<id>-short-name`) and commit, with a unit test where practical.

---

## Phase 0 — Setup

- [x] **S1. Local dev environment.** Install Python 3.12, create `.venv`, `pip install -e .`, confirm `pytest` passes on the current code.
- [x] **S2. Move test deps out of runtime deps.** `pytest` / `pytest-mock` → `[project.optional-dependencies] dev` in `pyproject.toml`. Fix the `[tools.setuptools...]` typo (should be `[tool.setuptools...]`).

## Phase 1 — Make it work again (critical)

- [x] **B1. Spotify playlist entries renamed `track` → `item`.** _(unit-tested; live test pending a Spotify dev app)_ Every playlist sync crashes with `KeyError: 'track'`.
  - `sync.py` `_fetch_all_from_spotify_in_chunks` and the `fields` string in `get_tracks_from_spotify_playlist`.
  - Fix: request `items(item(...))`, read `entry.get('item')`, fall back to `entry.get('track')`, accept only dicts (`track` can now be the boolean `true`). Liked Songs (`/me/tracks`) still use `track` — the helper must handle both.
  - Pin `spotipy~=2.26` (first version using `/playlists/{id}/items`).
- [x] **B2. Retry only transient errors.** `repeat_on_request_error` retries every error (incl. 403/404) with ~16 min of sleeps, then `sys.exit(1)`.
  - Retry only on 429 / 5xx / network errors; respect `Retry-After`; on `QUOTA_EXCEEDED` stop retrying.
  - On 403/404 for a playlist: log it, skip that playlist, continue the run (playlists you don't own now return 403).
  - Replace `time.sleep` with `await asyncio.sleep` so one retry doesn't freeze all concurrent searches.
- [x] **B3. Tidal `412` ETag errors on playlist writes.** Sync aborts mid-rewrite, can leave a playlist half-erased.
  - `tidalapi_patch.py` `_remove_indices_from_playlist`, `add_multiple_tracks_to_playlist`.
  - Fix: on 412 only — refresh playlist/ETag, exponential back-off, retry up to 5 times; build headers inside the retried call.
- [x] **B4. Unavailable Tidal album crashes the search.** `album.tracks()` can raise `ObjectNotFound`; the `assert` in `_search_for_track_in_album` can fire.
  - Fix: catch `ObjectNotFound` per album and move on; replace the `assert` with `continue`.
- [x] **B5. Race condition in parallel Tidal pagination.** `_get_all_chunks` mutates one shared `params` dict across threads → wrong offsets → duplicate/missing tracks. Also a mutable default argument.
  - Fix: `new_params = {**params, 'offset': offset}`; default `params=None`.

## Phase 2 — Reliability

- [x] **B6. SQLite "database is locked".** Many threads write `.cache.db` concurrently.
  - Fix: enable WAL mode + busy timeout on the engine (or serialise writes through a lock).
- [x] **B7. Failure back-off never grows.** `cache_match_failure` calls `_get_next_retry_time()` without `insert_time`, so re-failures always wait 7 days instead of doubling.
  - Fix: pass the existing row's `insert_time`; add a test.
- [x] **B8. Rate limiter can over-release.** `_run_rate_limiter` calls `Semaphore.release()` without a cap, so after idle periods bursts exceed `max_concurrency`.
  - Fix: only release up to the configured capacity.
- [x] **B9. Spotify login error message broken.** `auth.py` `.format()` without `{}`; `requests_timeout=2` is too aggressive (raise to ~10).
- [x] **B10. "songs not found.txt" gets empty headers.** Only write the playlist header when there are missing songs.

## Phase 3 — Matching improvements

- [x] **I1. Look tracks up by ISRC first.** _(unit-tested; live test pending)_ One exact lookup before the album/track text searches; finds tracks Tidal spells differently and saves requests. (Verify `tidalapi` ISRC lookup works with our session/client.)
- [x] **I2. Don't truncate artist names at hyphens.** `simple()` turns `Ken-Y` into `Ken`; use it for titles only, a gentler normaliser for artists.
- [x] **I3. Collect a "why didn't it match" report.** Record the closest candidate per missing track to make tuning easier.
- [x] **I4. Title matching is too loose.** `name_match` accepts the Spotify title anywhere inside the Tidal title, so "Song" matches "Different Song" (with same artist and duration ±2s). Match on word boundaries / similarity instead.

## Phase 4 — Prepare the core for a UI

The sync code currently assumes one user, a terminal, and files in the working directory.

- [x] **R1. No `sys.exit` / `print` in library code.** Raise typed exceptions; report progress through a callback/event interface (CLI keeps `tqdm` via an adapter).
- [ ] **R2. Per-user state behind a storage interface.** Spotify/Tidal tokens, config, match-failure cache passed in explicitly instead of `config.yml` / `.session.yml` / `.cache.db` in the CWD. Container disks are ephemeral (H1), so state goes through a storage interface: local files for the CLI, D1/R2 (or Durable Object storage) in production. Remove module-level singletons (`TrackMatchCache.data` is a class attribute shared by all instances).
- [ ] **R3. Pluggable auth flows.** Spotify OAuth with a hosted HTTPS redirect URI; Tidal device-login returning the link/code to the UI instead of opening a browser.
- [ ] **R4. Playlist pairing by stored ID**, not by name, so renames don't create duplicates.

## Phase 5 — Web UI & hosting (Cloudflare)

- [x] **H1. Decide architecture.** _Decided 2026-10-24: (a) Cloudflare Containers running the Python app._ Python Workers can't run threads or `requests`; a TypeScript rewrite would depend on TIDAL's official API, whose playlist/collection write support and throttling are unproven. Containers: Workers Paid ($5/mo), ephemeral disk, reached via Worker → Durable Object, sleep after inactivity (default 10 min). Revisit the official TIDAL API later (tidalapi uses TIDAL's own client credentials).
- [ ] **H0. Container sleep spike.** Deploy a throwaway container running a fake 20-minute job while a page polls its progress; confirm the instance isn't stopped mid-job (else keep it alive from the Durable Object). Needs a Cloudflare account on Workers Paid.
- [ ] **H2. Build UI:** log in with Spotify + Tidal, pick playlists, start sync, live progress, "not found" list.
- [ ] **H3. Secrets & data:** token encryption at rest, per-user storage, rate limits shared across users.
- [ ] **H4. Spotify app limits:** owner needs Premium; Dev Mode user cap (5 for apps created after 2026-02-11) — add friends' emails in the Spotify dashboard.

---

## Known external constraints

- Spotify Dev Mode (Feb–Mar 2026): playlist contents readable only for playlists you own or collaborate on; `/playlists/{id}/tracks` → `/items`; app owner needs Premium.
- Tidal: `tidalapi` is unofficial; breakages are fixed upstream at EbbLabs/python-tidal — keep the dependency current.
