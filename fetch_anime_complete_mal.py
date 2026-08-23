#!/usr/bin/env python3
"""
Anime Complete Fetcher  (MAL Official API version, with relations)
====================================================================
Primary source : MAL Official API v2  (https://myanimelist.net/apiconfig)
AniList lookup : MAL ID -> AniList ID only
Relations      : related_anime field -> parent series (prequel/parent_story/etc.)

COVERAGE STRATEGY
------------------
MAL's official API has no plain "browse all anime by ID" endpoint (unlike
Jikan). To get FULL database coverage we instead enumerate every anime
season from 1917 (earliest anime on MAL) through next year, using
/anime/season/{year}/{season}, and paginate each season fully. This is
the same approach MAL's own website uses to let you browse "all anime",
and in practice covers effectively the entire database. A small number
of entries with no season/date at all (a handful of very obscure or
never-formally-scheduled titles) may be missed - there is no public
endpoint that exposes those outside of ranking/search, which are not
exhaustive either.

RATE LIMITING
-------------
MAL's official API is much lighter on rate limiting than Jikan in
practice (Jikan proxies MyAnimeList itself and enforces ~60 req/min /
3 req/sec with frequent 429s). This script uses 1 request/second
against MAL directly, with 429 handling (Retry-After) and exponential
backoff on 5xx/connection errors as a safety net.

Requires a MAL API client_id. Set it via the MAL_CLIENT_ID environment
variable, or edit CLIENT_ID below directly.

    export MAL_CLIENT_ID="your_client_id_here"
    python3 fetch_anime_complete_mal.py

Note: client_id alone is enough for these public read-only endpoints
(anime list / anime details / related_anime). An OAuth access_token is
only required for endpoints that modify a user's list.

Output: anime_all_complete.json
"""

import json, time, os, sys, requests
from collections import defaultdict

MAL_API          = "https://api.myanimelist.net/v2"
ANILIST_API      = "https://graphql.anilist.co"
CLIENT_ID        = os.environ.get("MAL_CLIENT_ID", "")
MAL_INTERVAL     = 1.0     # be polite - MAL doesn't publish an official number
ANILIST_INTERVAL = 0.72
OUTPUT_FILE      = "anime_all_complete.json"
CHECKPOINT_FILE  = "checkpoint_anime.json"
ANILIST_BATCH    = 50
PAGE_LIMIT       = 100     # MAL max limit per page for the anime list endpoint

# Fields requested for the /anime/ranking (list) call - keep this light,
# we fetch full detail (incl. related_anime) per-anime separately.
LIST_FIELDS = "id,title,alternative_titles,media_type,status"

# Fields requested for the /anime/{id} detail call
DETAIL_FIELDS = (
    "id,title,alternative_titles,start_date,end_date,synopsis,mean,"
    "rank,popularity,num_list_users,num_scoring_users,nsfw,genres,"
    "media_type,status,num_episodes,start_season,broadcast,source,"
    "average_episode_duration,rating,studios,main_picture,statistics,"
    "related_anime"
)

_last_mal = _last_anilist = 0.0


def mal_get(path, params=None):
    global _last_mal
    if not CLIENT_ID:
        print("[Fatal] MAL_CLIENT_ID is not set. Export MAL_CLIENT_ID or edit CLIENT_ID in the script.")
        sys.exit(1)
    elapsed = time.monotonic() - _last_mal
    if elapsed < MAL_INTERVAL:
        time.sleep(MAL_INTERVAL - elapsed)
    url = f"{MAL_API}{path}"
    headers = {"X-MAL-CLIENT-ID": CLIENT_ID}
    for attempt in range(8):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=30)
            _last_mal = time.monotonic()
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 10)))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                print(f"\n[Fatal] MAL API auth error ({r.status_code}): {r.text[:200]}")
                sys.exit(1)
            if r.status_code >= 500:
                time.sleep(10 * (attempt + 1)); continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as exc:
            print(f"[mal_get error, attempt {attempt+1}] {exc}")
            time.sleep(8 * (attempt + 1))
    return None


ANILIST_QUERY = """
query ($malIds: [Int]) {
  Page(perPage: 50) {
    media(idMal_in: $malIds, type: ANIME) { id idMal }
  }
}
"""

def anilist_lookup(mal_ids):
    global _last_anilist
    elapsed = time.monotonic() - _last_anilist
    if elapsed < ANILIST_INTERVAL:
        time.sleep(ANILIST_INTERVAL - elapsed)
    for attempt in range(6):
        try:
            r = requests.post(ANILIST_API,
                json={"query": ANILIST_QUERY, "variables": {"malIds": mal_ids}},
                timeout=30,
                headers={"Content-Type": "application/json", "Accept": "application/json"})
            _last_anilist = time.monotonic()
            if r.status_code == 429:
                time.sleep(int(r.headers.get("Retry-After", 60))); continue
            if r.status_code >= 500:
                time.sleep(15 * (attempt + 1)); continue
            r.raise_for_status()
            result = {}
            for m in (r.json().get("data", {}).get("Page", {}).get("media", []) or []):
                if m.get("idMal") and m.get("id"):
                    result[m["idMal"]] = m["id"]
            return result
        except requests.exceptions.RequestException:
            time.sleep(8 * (attempt + 1))
    return {}


# MAL media_type -> internal category
FORMAT_CATEGORY = {
    "tv": "TV", "tv_special": "TV",
    "movie": "MOVIE", "special": "SPECIAL",
    "ova": "OVA", "ona": "ONA", "music": "MUSIC",
}
CATEGORY_ORDER = ["TV", "SPECIAL", "OVA", "MOVIE", "ONA", "MUSIC", "UNKNOWN"]

# MAL relation_type values that indicate a parent series
# (see https://myanimelist.net/apiconfig/references/api/v2 - related_anime)
PARENT_RELATIONS = {"prequel", "parent_story", "alternative_setting", "alternative_version"}


def parse_date(d):
    if not d:
        return {}
    p = d.split("-")
    try:
        return {
            "year":  int(p[0]) if len(p) > 0 else None,
            "month": int(p[1]) if len(p) > 1 else None,
            "day":   int(p[2]) if len(p) > 2 else None,
        }
    except (ValueError, IndexError):
        return {}


def make_air_date(sd):
    y = sd.get("year")
    if not y:
        return None
    m = sd.get("month"); d = sd.get("day")
    return f"{y}-{str(m).zfill(2) if m else '01'}-{str(d).zfill(2) if d else '01'}"


def build_entry(anime, anilist_id):
    mal_id  = anime.get("id")
    title_block = anime.get("alternative_titles") or {}
    romaji  = anime.get("title")
    english = title_block.get("en") or None
    native  = title_block.get("ja") or None

    fmt     = (anime.get("media_type") or "").lower()
    cat     = FORMAT_CATEGORY.get(fmt, "UNKNOWN")

    sd      = parse_date(anime.get("start_date"))
    ed      = parse_date(anime.get("end_date"))

    picture = anime.get("main_picture") or {}
    cover   = picture.get("large") or picture.get("medium")

    duration_sec = anime.get("average_episode_duration")
    duration = f"{duration_sec // 60} min" if duration_sec else None

    season_block = anime.get("start_season") or {}

    stats = (anime.get("statistics") or {}).get("num_list_users")

    # related_anime -> parent_mal_ids
    parent_mal_ids = []
    for rel in (anime.get("related_anime") or []):
        if rel.get("relation_type") in PARENT_RELATIONS:
            node = rel.get("node") or {}
            if node.get("id"):
                parent_mal_ids.append(node["id"])

    return {
        "mal_id":         mal_id,
        "anilist_id":     anilist_id,
        "title_romaji":   romaji,
        "title_english":  english,
        "title_native":   native,
        "format":         fmt,
        "category":       cat,
        "status":         anime.get("status"),
        "episodes":       anime.get("num_episodes"),
        "duration":       duration,
        "start_date":     sd,
        "end_date":       ed,
        "air_date":       make_air_date(sd),
        "season":         season_block.get("season"),
        "season_year":    season_block.get("year"),
        "score":          anime.get("mean"),
        "scored_by":      anime.get("num_scoring_users"),
        "rank":           anime.get("rank"),
        "popularity":     anime.get("popularity"),
        "members":        anime.get("num_list_users") or stats,
        "favorites":      None,  # not exposed by MAL official API
        "genres":         [g["name"] for g in (anime.get("genres") or []) if g.get("name")],
        "studios":        [s["name"] for s in (anime.get("studios") or []) if s.get("name")],
        "source":         anime.get("source"),
        "rating":         anime.get("rating"),
        "synopsis":       anime.get("synopsis"),
        "cover_image":    cover,
        "trailer_url":    None,  # not exposed by MAL official API
        "parent_mal_ids": parent_mal_ids,
    }


SEASONS = ["winter", "spring", "summer", "fall"]
START_YEAR = 1917   # earliest anime on MAL
import datetime
END_YEAR = datetime.date.today().year + 1  # include upcoming season


def season_sequence():
    """Yield every (year, season) pair from START_YEAR to END_YEAR."""
    for year in range(START_YEAR, END_YEAR + 1):
        for season in SEASONS:
            yield year, season


def load_checkpoint():
    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                ck = json.load(f)
            print(f"\n[Resume] season_index={ck['season_index']}  entries={len(ck['entries'])}\n")
            return ck["season_index"], ck["entries"]
        except Exception as exc:
            print(f"[Warning] Checkpoint corrupt ({exc}) - starting fresh\n")
    return 0, []


def save_checkpoint(season_index, entries):
    tmp = CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"season_index": season_index, "fetched": len(entries), "entries": entries},
                   f, ensure_ascii=False)
    os.replace(tmp, CHECKPOINT_FILE)


def save_output(entries):
    print("\nBuilding final JSON ...")
    cats = defaultdict(list)
    seen = set()
    for e in entries:
        mid = e["mal_id"]
        if mid not in seen:
            seen.add(mid)
            cats[e["category"]].append(e)

    for cat in cats:
        cats[cat].sort(key=lambda e: (
            (e["start_date"] or {}).get("year")  or 9999,
            (e["start_date"] or {}).get("month") or 99,
            (e["start_date"] or {}).get("day")   or 99,
        ))

    has_anilist = sum(1 for e in entries if e.get("anilist_id"))
    output = {
        "meta": {
            "fetched_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_fetched":   len(seen),
            "with_anilist_id": has_anilist,
            "source":          "MAL Official API v2 + AniList ID lookup + related_anime",
            "categories":      {cat: len(cats[cat]) for cat in CATEGORY_ORDER if cat in cats},
        },
        "by_category": {cat: cats[cat] for cat in CATEGORY_ORDER if cat in cats},
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n=> Saved : {OUTPUT_FILE}")
    print(f"   Total      : {len(seen)}")
    print(f"   AniList IDs: {has_anilist}")
    for cat in CATEGORY_ORDER:
        if cat in cats:
            print(f"   {cat:10s}: {len(cats[cat])}")


def fetch_season_all_pages(year, season):
    """Fetch every anime node for a given (year, season), following pagination.
    Returns [] for a season with no anime (valid), or None on a hard failure."""
    nodes = []
    offset = 0
    while True:
        data = mal_get(f"/anime/season/{year}/{season}", params={
            "limit": PAGE_LIMIT,
            "offset": offset,
            "fields": LIST_FIELDS,
            "sort": "anime_score",
        })
        if data is None:
            return None  # signals a hard failure to the caller
        page_nodes = [d["node"] for d in data.get("data", []) if d.get("node")]
        nodes.extend(page_nodes)
        has_next = bool((data.get("paging") or {}).get("next"))
        if not has_next or not page_nodes:
            break
        offset += PAGE_LIMIT
    return nodes


def main():
    print("Anime Complete Fetcher  (MAL Official API + related_anime + AniList ID)")
    print(f"Output : {OUTPUT_FILE}")
    print(f"Coverage: {START_YEAR} .. {END_YEAR}, all 4 seasons/year\n")

    if not CLIENT_ID:
        print("[Fatal] Set MAL_CLIENT_ID env var before running:")
        print('   export MAL_CLIENT_ID="your_client_id_here"')
        sys.exit(1)

    all_seasons = list(season_sequence())
    season_index, all_entries = load_checkpoint()
    existing_mal_ids = {e["mal_id"] for e in all_entries}

    MAX_SEASON_RETRIES = 5
    season_retry_count = 0

    try:
        while season_index < len(all_seasons):
            year, season = all_seasons[season_index]
            print(f"  [MAL] {year} {season} ...", end=" ", flush=True)

            anime_list = fetch_season_all_pages(year, season)
            if anime_list is None:
                season_retry_count += 1
                if season_retry_count >= MAX_SEASON_RETRIES:
                    print(f"failed {MAX_SEASON_RETRIES}x - skipping {year} {season} for now")
                    season_retry_count = 0
                    season_index += 1
                    save_checkpoint(season_index, all_entries)
                    continue
                print(f"failed ({season_retry_count}/{MAX_SEASON_RETRIES}) - retrying in 10s ...")
                time.sleep(10)
                continue  # retry same season_index
            season_retry_count = 0

            if not anime_list:
                print("no entries")
            else:
                new_anime = [a for a in anime_list
                             if a.get("id") not in existing_mal_ids]
                print(f"{len(anime_list)} found, {len(new_anime)} new  ", end="", flush=True)

                # Fetch full detail (includes related_anime) for each new entry
                full_anime = []
                for a in new_anime:
                    mid = a.get("id")
                    if not mid:
                        continue
                    detail = mal_get(f"/anime/{mid}", params={"fields": DETAIL_FIELDS})
                    full_anime.append(detail if detail else a)  # fallback: partial data

                # AniList bulk ID lookup
                mal_ids = [a["id"] for a in full_anime if a.get("id")]
                anilist_map = {}
                if mal_ids:
                    for i in range(0, len(mal_ids), ANILIST_BATCH):
                        anilist_map.update(anilist_lookup(mal_ids[i:i + ANILIST_BATCH]))

                for anime in full_anime:
                    mid = anime.get("id")
                    entry = build_entry(anime, anilist_map.get(mid) if mid else None)
                    all_entries.append(entry)
                    existing_mal_ids.add(mid)

                found_al = sum(1 for a in full_anime if anilist_map.get(a.get("id")))
                print(f"| AniList: {found_al}/{len(new_anime)} | total: {len(all_entries)}")

            season_index += 1
            save_checkpoint(season_index, all_entries)

    except KeyboardInterrupt:
        print("\n\n[Paused] Saving checkpoint ...")
        save_checkpoint(season_index, all_entries)
        print("[OK] Run again to resume.")
        sys.exit(0)

    print("\nAll seasons processed - fetching complete!")
    save_output(all_entries)
    if os.path.exists(CHECKPOINT_FILE):
        os.remove(CHECKPOINT_FILE)
        print("Checkpoint cleaned up.")


if __name__ == "__main__":
    main()