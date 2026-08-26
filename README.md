# Anime Complete Fetcher - GitHub Actions

This package runs the supplied MAL Official API v2 anime fetcher through GitHub Actions.

## Setup

1. Upload these files to your GitHub repository.
2. Go to:
   Settings -> Secrets and variables -> Actions
3. Add a repository secret:
   `MAL_CLIENT_ID`
4. Put your MyAnimeList API Client ID as the secret value.
5. Open:
   Actions -> Fetch Anime Database
6. Use `Run workflow` for a manual run.

The workflow checks daily and performs an automatic update once every three
days at 02:00 UTC. A manual `Run workflow` always starts an update immediately.

The script uses MAL Official API v2 and AniList only for MAL ID -> AniList ID lookup.


## v2 resumable / future-season-safe behavior

This build now persists:
- `checkpoint_anime.json`
- `mal_season_cache.json`
- `anilist_lookup_cache.json`

Completed MAL seasons are cached immediately. Future seasonal endpoints that
return HTTP 404 are skipped for the current run but are NOT cached as complete,
so later scheduled runs can retry them after MAL publishes the season.

AniList MAL->AniList lookups are cached as well, including misses, and the
GitHub Actions cache is saved with `if: always()` so completed work survives
workflow failures/timeouts.


## v3 resilient MAL networking

- MAL pacing uses a fast 0.45s global interval, with authoritative 429
  Retry-After handling and exponential backoff.
- SSL/connection/read-timeout failures use bounded exponential backoff + jitter.
- 429 honors Retry-After and logs the exact wait.
- Detail requests get 10 attempts and a 45s read timeout.
- Successful MAL detail responses are persisted in `mal_detail_cache.json`.
- Cache is saved every 10 detail records.
- Transiently failed details stay `pending`; they are not silently written as incomplete records.
- The next run retries pending IDs.
- GitHub Actions caches the detail cache and runs four resumable 4h 50m fetch
  chunks, providing an approximately 20-hour fetch window without exceeding
  GitHub-hosted runners' per-job time limit.
- Redundant competing checkpoint-cache steps and duplicate YAML `if:` keys were fixed.


## v4 complete MAL relation graph

The fetcher now preserves every edge returned by MAL Official API v2 `related_anime` instead of keeping only parent-like relations. Each anime entry includes:

- `relations`: the raw MAL relation edges with MAL ID and relation type.
- `relation_mal_ids`: direct relation IDs, normalized bidirectionally for strong franchise links.
- `parent_mal_ids`: retained for backward compatibility.
- `franchise_mal_ids`: recursively connected franchise entries.
- `related_movies`: all connected `MOVIE` MAL IDs.
- `related_ovas`: all connected `OVA` MAL IDs.
- `related_onas`: all connected `ONA` MAL IDs.
- `related_specials`: all connected `SPECIAL` MAL IDs.

Recursive traversal uses sequel, prequel, side-story, parent-story, summary, full-story, spin-off, alternative-setting, and alternative-version edges. Weak crossover-style links are still preserved in `relations`, but are not allowed to merge whole franchises.

Because v3 checkpoints discarded relation edges, v4 uses a new checkpoint/cache schema and intentionally performs a clean MAL detail rebuild on the first v4 run.


## Incremental updates (no full restart)

After the first complete build, scheduled and manual runs load the committed
`anime_all_complete.json` as their baseline. They do not start again at 1917.

Each update:

- scans every lightweight MAL seasonal listing from 1917 through next year,
  ensuring newly indexed anime are discovered even in an old season;
- performs a full detail check of every existing record, including undated and
  very old finished anime;
- computes a stable SHA-256 hash over source fields and replaces a record only
  when its fetched content changed;
- adds every MAL ID missing from the old JSON;
- rebuilds the relation/franchise indexes from the merged database;
- keeps a checkpoint and resumes the same update plan when a job times out.

If Part 1, Part 2, or Part 3 completes the update, later parts detect the final
JSON and skip duplicate fetching. The next three-day or manual run begins a new
incremental update from the committed JSON, not a clean rebuild.

Publishing uses a clean temporary worktree created from the latest
`origin/main`. This preserves commits pushed while the long fetch was running
and prevents the old-checkout non-fast-forward failure. The final short publish
window is retried up to three times if `main` moves concurrently.
