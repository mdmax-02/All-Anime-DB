#!/usr/bin/env python3
"""
Anime Complete Fetcher  (MAL Official API version, with relations)
====================================================================
Primary source : MAL Official API v2  (https://myanimelist.net/apiconfig)
AniList lookup : MAL ID -> AniList ID only
Relations      : full related_anime graph (all MAL relation edges preserved)

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

import json, time, os, sys, requests, random
from collections import defaultdict

MAL_API          = "https://api.myanimelist.net/v2"
ANILIST_API      = "https://graphql.anilist.co"
CLIENT_ID        = os.environ.get("MAL_CLIENT_ID", "")
MAL_INTERVAL     = 1.35     # be polite - MAL doesn't publish an official number
ANILIST_INTERVAL = 0.72
OUTPUT_FILE      = "anime_all_complete.json"
DATA_SCHEMA_VERSION = 4
CHECKPOINT_FILE  = "checkpoint_anime.json"
MAL_CACHE_FILE   = "mal_season_cache.json"
ANILIST_CACHE_FILE = "anilist_lookup_cache.json"
MAL_DETAIL_CACHE_FILE = "mal_detail_cache.json"
MAX_LEGACY_DETAIL_CACHE_BYTES = 64 * 1024 * 1024
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


def load_json_cache(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[Warning] Could not load cache {path}: {exc}")
        return default


def save_json_cache(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_compact_detail_cache(completed_mal_ids):
    """Load only resumable details that are not already in the checkpoint.

    Older versions accumulated every fetched MAL response forever. That file
    can grow to hundreds of MB and make a resumed Actions run appear frozen
    immediately after the [Resume] line. Completed entries already contain
    those details, so an oversized legacy cache is safe to discard; at worst
    the currently incomplete season is fetched again.
    """
    if not os.path.exists(MAL_DETAIL_CACHE_FILE):
        print("[Resume] No detail cache; continuing from checkpoint.", flush=True)
        return {"version": 2, "details": {}, "pending": []}

    size = os.path.getsize(MAL_DETAIL_CACHE_FILE)
    if size > MAX_LEGACY_DETAIL_CACHE_BYTES:
        print(
            f"[Resume] Legacy detail cache is {size / 1024 / 1024:.1f} MiB; "
            "skipping it to avoid the startup stall. The current season will be refetched.",
            flush=True,
        )
        compact = {"version": 2, "details": {}, "pending": []}
        save_json_cache(MAL_DETAIL_CACHE_FILE, compact)
        return compact

    print(f"[Resume] Loading detail cache ({size / 1024 / 1024:.1f} MiB) ...", flush=True)
    cache = load_json_cache(MAL_DETAIL_CACHE_FILE, {"version": 2, "details": {}, "pending": []})
    old_details = cache.get("details") or {}
    details = {
        str(mid): detail
        for mid, detail in old_details.items()
        if int(mid) not in completed_mal_ids
    }
    pending = [mid for mid in (cache.get("pending") or []) if mid not in completed_mal_ids]
    compact = {"version": 2, "details": details, "pending": pending}
    if len(details) != len(old_details) or cache.get("version") != 2:
        save_json_cache(MAL_DETAIL_CACHE_FILE, compact)
    print(f"[Resume] Detail cache ready: {len(details)} unfinished records.", flush=True)
    return compact


def mal_get(path, params=None):
    """Resilient MAL GET. Returns (status, data): ok/not_found/failed."""
    global _last_mal
    if not CLIENT_ID:
        print("[Fatal] MAL_CLIENT_ID is not set.")
        sys.exit(1)

    url = f"{MAL_API}{path}"
    headers = {
        "X-MAL-CLIENT-ID": CLIENT_ID,
        "Accept": "application/json",
        "User-Agent": "clix-anime-mappings/3.0",
        "Connection": "close",
    }

    for attempt in range(10):
        elapsed = time.monotonic() - _last_mal
        if elapsed < MAL_INTERVAL:
            time.sleep(MAL_INTERVAL - elapsed)

        try:
            r = requests.get(url, params=params, headers=headers, timeout=(10, 45))
            _last_mal = time.monotonic()

            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After") or 30)
                wait = max(retry, min(120, 10 * (attempt + 1))) + random.uniform(0.5, 2.5)
                print(f"\n[MAL 429] {path} - waiting {wait:.0f}s (attempt {attempt+1}/10)", flush=True)
                time.sleep(wait)
                continue

            if r.status_code == 404:
                return "not_found", None

            if r.status_code in (401, 403):
                print(f"\n[Fatal] MAL API auth error ({r.status_code}): {r.text[:200]}")
                sys.exit(1)

            if r.status_code >= 500:
                wait = min(120, 8 * (2 ** min(attempt, 4))) + random.uniform(0, 3)
                print(f"\n[MAL {r.status_code}] {path} - retrying in {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue

            r.raise_for_status()
            return "ok", r.json()

        except (requests.exceptions.SSLError,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as exc:
            wait = min(120, 6 * (2 ** min(attempt, 4))) + random.uniform(0, 4)
            print(
                f"\n[MAL transient] {path} attempt {attempt+1}/10: "
                f"{type(exc).__name__}; retrying in {wait:.0f}s",
                flush=True,
            )
            time.sleep(wait)
        except requests.exceptions.RequestException as exc:
            wait = min(90, 8 * (attempt + 1)) + random.uniform(0, 3)
            print(f"\n[MAL request error] {path} attempt {attempt+1}/10: {exc}; waiting {wait:.0f}s", flush=True)
            time.sleep(wait)

    print(f"\n[MAL pending] {path} failed after 10 attempts; will retry on next run.", flush=True)
    return "failed", None


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

# MAL relation types used to build a franchise graph.
# We preserve *all* related_anime edges in each entry, but exclude weak/crossover
# links from recursive franchise traversal to avoid merging unrelated franchises.
PARENT_RELATIONS = {"prequel", "parent_story", "alternative_setting", "alternative_version"}
FRANCHISE_RELATIONS = {
    "sequel", "prequel", "alternative_setting", "alternative_version",
    "side_story", "parent_story", "summary", "full_story", "spin_off",
}


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

    # Preserve the COMPLETE MAL related_anime payload instead of throwing away
    # sequel/side_story/summary/spin_off edges. parent_mal_ids is retained for
    # backward compatibility with existing consumers.
    relations = []
    relation_mal_ids = []
    parent_mal_ids = []
    seen_rel_ids = set()
    for rel in (anime.get("related_anime") or []):
        node = rel.get("node") or {}
        rel_id = node.get("id")
        if not rel_id:
            continue

        rel_type = rel.get("relation_type")
        relations.append({
            "mal_id": rel_id,
            "relation_type": rel_type,
            "relation_type_formatted": rel.get("relation_type_formatted"),
        })

        if rel_id not in seen_rel_ids:
            seen_rel_ids.add(rel_id)
            relation_mal_ids.append(rel_id)

        if rel_type in PARENT_RELATIONS and rel_id not in parent_mal_ids:
            parent_mal_ids.append(rel_id)

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
        "relations":       relations,
        "relation_mal_ids": relation_mal_ids,
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
            if ck.get("schema_version") != DATA_SCHEMA_VERSION:
                print(
                    f"[Resume] Old checkpoint schema {ck.get('schema_version')} detected; "
                    f"relation schema v{DATA_SCHEMA_VERSION} requires a clean rebuild.",
                    flush=True,
                )
                return 0, []
            print(f"\n[Resume] season_index={ck['season_index']}  entries={len(ck['entries'])}\n", flush=True)
            return ck["season_index"], ck["entries"]
        except Exception as exc:
            print(f"[Warning] Checkpoint corrupt ({exc}) - starting fresh\n")
    return 0, []


def save_checkpoint(season_index, entries):
    tmp = CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({
            "schema_version": DATA_SCHEMA_VERSION,
            "season_index": season_index,
            "fetched": len(entries),
            "entries": entries,
        }, f, ensure_ascii=False)
    os.replace(tmp, CHECKPOINT_FILE)


def _build_complete_relation_graph(entries):
    """Make MAL relations bidirectional and build recursive franchise components.

    MAL detail responses are directional. If A says B is a sequel but B omits
    the reverse edge (or the old parent-only logic would have discarded it),
    we still connect A<->B. Recursive traversal uses only strong franchise
    relation types; raw `relations` continues to preserve every MAL edge.
    """
    by_id = {e["mal_id"]: e for e in entries if e.get("mal_id")}
    graph = defaultdict(set)

    for entry in entries:
        src = entry.get("mal_id")
        if not src:
            continue
        for rel in entry.get("relations") or []:
            dst = rel.get("mal_id")
            rel_type = rel.get("relation_type")
            if dst and rel_type in FRANCHISE_RELATIONS:
                graph[src].add(dst)
                graph[dst].add(src)

    # Ensure direct relation ids expose reverse links too. This is intentionally
    # separate from raw `relations`, which remains the exact MAL response.
    for mid, entry in by_id.items():
        direct = set(entry.get("relation_mal_ids") or [])
        for neighbor in graph.get(mid, ()):
            direct.add(neighbor)
        entry["relation_mal_ids"] = sorted(direct)

    visited = set()
    for start_id in by_id:
        if start_id in visited:
            continue

        stack = [start_id]
        component = set()
        while stack:
            cur = stack.pop()
            if cur in component:
                continue
            component.add(cur)
            for nxt in graph.get(cur, ()):
                if nxt in by_id and nxt not in component:
                    stack.append(nxt)

        visited.update(component)
        component_sorted = sorted(component)

        # Precompute the four relation buckets ClixArena needs most often.
        buckets = {"MOVIE": [], "OVA": [], "ONA": [], "SPECIAL": []}
        for related_id in component_sorted:
            related = by_id.get(related_id)
            if not related:
                continue
            cat = related.get("category")
            if cat in buckets:
                buckets[cat].append(related_id)

        for mid in component:
            entry = by_id[mid]
            entry["franchise_mal_ids"] = [x for x in component_sorted if x != mid]
            entry["related_movies"] = [x for x in buckets["MOVIE"] if x != mid]
            entry["related_ovas"] = [x for x in buckets["OVA"] if x != mid]
            entry["related_onas"] = [x for x in buckets["ONA"] if x != mid]
            entry["related_specials"] = [x for x in buckets["SPECIAL"] if x != mid]


def save_output(entries):
    print("\nBuilding complete bidirectional relation graph ...")

    # Deduplicate first so relation graph and category output use the same rows.
    unique_entries = []
    seen = set()
    for e in entries:
        mid = e["mal_id"]
        if mid not in seen:
            seen.add(mid)
            unique_entries.append(e)

    _build_complete_relation_graph(unique_entries)

    cats = defaultdict(list)
    for e in unique_entries:
        cats[e["category"]].append(e)

    for cat in cats:
        cats[cat].sort(key=lambda e: (
            (e["start_date"] or {}).get("year")  or 9999,
            (e["start_date"] or {}).get("month") or 99,
            (e["start_date"] or {}).get("day")   or 99,
        ))

    has_anilist = sum(1 for e in unique_entries if e.get("anilist_id"))
    with_relations = sum(1 for e in unique_entries if e.get("relation_mal_ids"))
    output = {
        "meta": {
            "schema_version":    DATA_SCHEMA_VERSION,
            "fetched_at":        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "total_fetched":     len(seen),
            "with_anilist_id":   has_anilist,
            "with_relations":    with_relations,
            "source":            "MAL Official API v2 + AniList ID lookup + complete related_anime graph",
            "categories":        {cat: len(cats[cat]) for cat in CATEGORY_ORDER if cat in cats},
        },
        "by_category": {cat: cats[cat] for cat in CATEGORY_ORDER if cat in cats},
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n=> Saved : {OUTPUT_FILE}")
    print(f"   Total         : {len(seen)}")
    print(f"   AniList IDs   : {has_anilist}")
    print(f"   With relations: {with_relations}")
    for cat in CATEGORY_ORDER:
        if cat in cats:
            print(f"   {cat:10s}: {len(cats[cat])}")


def fetch_season_all_pages(year, season):
    """
    Fetch a season with persistent per-season caching.

    Return values:
      ("ok", nodes)          successfully fetched/cached season
      ("unavailable", [])    future/unpublished MAL season (HTTP 404)
      ("failed", [])         hard failure after retries

    Future 404 seasons are deliberately NOT cached as completed so a later
    scheduled run can discover them when MAL publishes the endpoint.
    """
    cache = load_json_cache(MAL_CACHE_FILE, {"version": 1, "seasons": {}})
    seasons = cache.setdefault("seasons", {})
    key = f"{year}-{season}"

    cached = seasons.get(key)
    if isinstance(cached, dict) and cached.get("status") == "complete":
        return "ok", cached.get("nodes", [])

    nodes = []
    offset = 0

    while True:
        req_status, data = mal_get(f"/anime/season/{year}/{season}", params={
            "limit": PAGE_LIMIT,
            "offset": offset,
            "fields": LIST_FIELDS,
            "sort": "anime_score",
        })

        if req_status == "not_found":
            return "unavailable", []
        if req_status == "failed":
            return "failed", []

        page_nodes = [d["node"] for d in data.get("data", []) if d.get("node")]
        nodes.extend(page_nodes)

        has_next = bool((data.get("paging") or {}).get("next"))
        if not has_next or not page_nodes:
            break

        offset += PAGE_LIMIT

    # Persist immediately after this season completes.
    seasons[key] = {
        "status": "complete",
        "nodes": nodes,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_json_cache(MAL_CACHE_FILE, cache)
    return "ok", nodes


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

    # Resume intelligently: completed seasons live in MAL_CACHE_FILE.
    # If the old linear checkpoint advanced past a future 404 season, rewind
    # only to the earliest season not present in the persistent completed cache.
    print("[Resume] Checking completed season index ...", flush=True)
    _season_cache = load_json_cache(MAL_CACHE_FILE, {"version": 1, "seasons": {}})
    _completed = (_season_cache.get("seasons") or {})
    earliest_missing_index = None
    for idx, (yy, ss) in enumerate(all_seasons):
        item = _completed.get(f"{yy}-{ss}")
        if not (isinstance(item, dict) and item.get("status") == "complete"):
            earliest_missing_index = idx
            break
    if earliest_missing_index is not None:
        season_index = min(season_index, earliest_missing_index)
    existing_mal_ids = {e["mal_id"] for e in all_entries}
    detail_cache = load_compact_detail_cache(existing_mal_ids)
    detail_map = detail_cache.setdefault("details", {})
    pending_detail_ids = set(detail_cache.get("pending") or [])

    MAX_SEASON_RETRIES = 5
    season_retry_count = 0

    try:
        while season_index < len(all_seasons):
            compact_after_checkpoint = []
            year, season = all_seasons[season_index]
            print(f"  [MAL] {year} {season} ...", end=" ", flush=True)

            season_status, anime_list = fetch_season_all_pages(year, season)

            if season_status == "unavailable":
                # Future/unpublished season. Do not mark it completed in the
                # persistent season cache. Continue this run, and future runs
                # will retry it automatically.
                print("not available yet - skipping for this run")
                season_retry_count = 0
                season_index += 1
                save_checkpoint(season_index, all_entries)
                continue

            if season_status == "failed":
                season_retry_count += 1
                if season_retry_count >= MAX_SEASON_RETRIES:
                    print(f"failed {MAX_SEASON_RETRIES}x - stopping safely")
                    save_checkpoint(season_index, all_entries)
                    return
                print(f"failed ({season_retry_count}/{MAX_SEASON_RETRIES}) - retrying in 10s ...")
                time.sleep(10)
                continue

            season_retry_count = 0

            if not anime_list:
                print("no entries")
            else:
                new_anime = [a for a in anime_list
                             if a.get("id") not in existing_mal_ids]
                print(f"{len(anime_list)} found, {len(new_anime)} new  ", end="", flush=True)

                # Fetch full detail with persistent per-MAL cache. A transient
                # network failure is kept pending instead of being permanently
                # written as a partial record.
                full_anime = []
                unresolved_this_season = []
                for n, a in enumerate(new_anime, 1):
                    mid = a.get("id")
                    if not mid:
                        continue

                    cached_detail = detail_map.get(str(mid))
                    if isinstance(cached_detail, dict) and cached_detail.get("id") == mid:
                        full_anime.append(cached_detail)
                        pending_detail_ids.discard(mid)
                        continue

                    detail_status, detail = mal_get(f"/anime/{mid}", params={"fields": DETAIL_FIELDS})
                    if detail_status == "ok" and isinstance(detail, dict):
                        detail_map[str(mid)] = detail
                        full_anime.append(detail)
                        pending_detail_ids.discard(mid)
                    elif detail_status == "not_found":
                        # Real deleted/private MAL entry: list data is the best
                        # authoritative data available.
                        full_anime.append(a)
                        pending_detail_ids.discard(mid)
                    else:
                        unresolved_this_season.append(mid)
                        pending_detail_ids.add(mid)

                    # Save progress inside large seasons so runner termination
                    # loses at most a small batch of detail requests.
                    if n % 10 == 0:
                        detail_cache["pending"] = sorted(pending_detail_ids)
                        save_json_cache(MAL_DETAIL_CACHE_FILE, detail_cache)

                detail_cache["pending"] = sorted(pending_detail_ids)
                save_json_cache(MAL_DETAIL_CACHE_FILE, detail_cache)

                if unresolved_this_season:
                    print(
                        f"\n    MAL detail pending: {len(unresolved_this_season)} "
                        f"(saved for retry; not written as partial records)",
                        flush=True,
                    )

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
                    compact_after_checkpoint.append(mid)

                found_al = sum(1 for a in full_anime if anilist_map.get(a.get("id")))
                print(f"| AniList: {found_al}/{len(full_anime)} | pending MAL detail: {len(unresolved_this_season)} | total: {len(all_entries)}")

            season_index += 1
            save_checkpoint(season_index, all_entries)

            # Once the full entry is durable in checkpoint_anime.json, its
            # raw detail response is redundant. Keeping only unfinished work
            # prevents the cache from growing without bound across years.
            if compact_after_checkpoint:
                for mid in compact_after_checkpoint:
                    detail_map.pop(str(mid), None)
                    pending_detail_ids.discard(mid)
                detail_cache["pending"] = sorted(pending_detail_ids)
                save_json_cache(MAL_DETAIL_CACHE_FILE, detail_cache)

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
