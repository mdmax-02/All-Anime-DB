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
