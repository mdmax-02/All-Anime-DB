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

The workflow also runs automatically every Sunday at 02:00 UTC.

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

- MAL pacing increased to 1.35s between requests.
- SSL/connection/read-timeout failures use bounded exponential backoff + jitter.
- 429 honors Retry-After and logs the exact wait.
- Detail requests get 10 attempts and a 45s read timeout.
- Successful MAL detail responses are persisted in `mal_detail_cache.json`.
- Cache is saved every 10 detail records.
- Transiently failed details stay `pending`; they are not silently written as incomplete records.
- The next run retries pending IDs.
- GitHub Actions caches the detail cache and keeps the 10-hour timeout.
- Redundant competing checkpoint-cache steps and duplicate YAML `if:` keys were fixed.
