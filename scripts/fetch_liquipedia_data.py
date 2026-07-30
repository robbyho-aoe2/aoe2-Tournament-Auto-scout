"""
Pulls AoE2 tournament + match data from Liquipedia and writes data/tournaments.json
and data/matches.json for the tracker page to fetch.

Meant to run on a schedule via .github/workflows/update-data.yml. Stdlib only,
nothing to install. To run locally:
    python scripts/fetch_liquipedia_data.py

Why this exists instead of the old action=askargs script: Liquipedia removed the
classic SMW `ask`/`askargs`/`cargoquery` API actions (verified live against
api.php?action=paraminfo - they're no longer in the action list). The free/basic
tier of their new structured API (api.liquipedia.net) is currently paused
("Enterprise only" per their own API page). What still works is the plain
MediaWiki API: action=query (page/category listing) and action=parse (raw
wikitext). Tournament bracket data lives in the wikitext itself as
{{Match |opponent1={{SoloOpponent|...}} |map1={{Map|civs1=..|civs2=..|map=..}} }}
blocks, so this script fetches wikitext and parses those templates directly.

Rate limits (Liquipedia API usage guidelines - bans are issued for violations):
  - 1 request / 2s for general API calls (action=query)
  - 1 request / 30s for action=parse specifically
"""
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API = "https://liquipedia.net/ageofempires/api.php"
USER_AGENT = "AoE2TournamentAutoScout/1.0 (robbyho@gmail.com) https://github.com/robbyho-aoe2/aoe2-Tournament-Auto-scout"

# ── Scope of the historical backfill. Widen TIERS or push SINCE_DATE back
#    whenever you want more history - just costs more 30s-spaced parse calls. ──
TIERS = ["S-Tier", "A-Tier", "B-Tier", "C-Tier"]
SINCE_DATE = "2023-01-01"

QUERY_INTERVAL = 2.0
PARSE_INTERVAL = 30.0

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

_last_query_ts = 0.0
_last_parse_ts = 0.0


def _throttled_get(params, interval, last_ts_holder):
    elapsed = time.monotonic() - last_ts_holder[0]
    if elapsed < interval:
        time.sleep(interval - elapsed)
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
    last_ts_holder[0] = time.monotonic()
    return json.loads(raw.decode("utf-8"))


_query_ts_holder = [0.0]
_parse_ts_holder = [0.0]


def api_query(**params):
    params["action"] = "query"
    params["format"] = "json"
    return _throttled_get(params, QUERY_INTERVAL, _query_ts_holder)


def api_parse_wikitext(page):
    data = _throttled_get(
        {"action": "parse", "page": page, "prop": "wikitext", "format": "json"},
        PARSE_INTERVAL, _parse_ts_holder,
    )
    if "error" in data:
        return None
    return data["parse"]["wikitext"]["*"]


def category_members(category):
    titles = set()
    cmcontinue = None
    while True:
        params = {"list": "categorymembers", "cmtitle": f"Category:{category}", "cmlimit": 500}
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        data = api_query(**params)
        for m in data.get("query", {}).get("categorymembers", []):
            titles.add(m["title"])
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        cmcontinue = cont
    return titles


EXCLUDE_TITLE_PATTERN = re.compile(r"qualifier|show ?match(es)?", re.IGNORECASE)


def discover_tournament_pages():
    """S-Tier (etc) AoE2 tournament pages, main-event only. Liquipedia has no
    single consistent naming convention for qualifiers/showmatches across
    tournaments ('/Qualifier', ': Replacement Qualifier', ': Last Chance
    Qualifier', ': Showmatch 2', ...), so this matches the word anywhere in
    the title rather than a fixed path pattern - matches the inspiration
    video's methodology of main-event-only, no qualifiers."""
    aoe2_pages = category_members("Age of Empires II Competitions")
    wanted = set()
    for tier in TIERS:
        wanted |= category_members(f"{tier} Tournaments")
    pages = sorted(wanted & aoe2_pages)
    pages = [p for p in pages if not EXCLUDE_TITLE_PATTERN.search(p)]
    return pages


# ── Civilization code -> full name, lifted from Module:CivLookup/data
#    (AoE2 section only - verified directly against the live module). ──
CIV_LOOKUP = {
    'arm': 'Armenians', 'azt': 'Aztecs', 'ber': 'Berbers', 'ben': 'Bengalis',
    'boh': 'Bohemians', 'bri': 'Britons', 'bul': 'Bulgarians', 'brg': 'Burgundians',
    'bur': 'Burmese', 'byz': 'Byzantines', 'cel': 'Celts', 'chi': 'Chinese',
    'cms': 'Cumans', 'cum': 'Cumans', 'dra': 'Dravidians', 'eth': 'Ethiopians',
    'fra': 'Franks', 'geo': 'Georgians', 'got': 'Goths', 'gur': 'Gurjaras',
    'hin': 'Hindustanis', 'hun': 'Huns', 'inc': 'Incas', 'ind': 'Indians',
    'ita': 'Italians', 'jap': 'Japanese', 'khm': 'Khmer', 'kor': 'Koreans',
    'lit': 'Lithuanians', 'mag': 'Magyars', 'mly': 'Malay', 'mal': 'Malians',
    'mli': 'Malians', 'may': 'Mayans', 'mon': 'Mongols', 'per': 'Persians',
    'pol': 'Poles', 'por': 'Portuguese', 'rom': 'Romans', 'sar': 'Saracens',
    'sic': 'Sicilians', 'sla': 'Slavs', 'slav': 'Slavs', 'spa': 'Spanish',
    'tat': 'Tatars', 'teu': 'Teutons', 'tur': 'Turks', 'vie': 'Vietnamese',
    'viet': 'Vietnamese', 'vik': 'Vikings',
}


NUMERIC_TIER = {"1": "S-Tier", "2": "A-Tier", "3": "B-Tier", "4": "C-Tier", "5": "D-Tier"}


def normalize_tier(raw):
    """Some tournament pages store |liquipediatier= as a bare number (1=S,
    2=A, ...) instead of the 'S-Tier' text - both forms show up across the
    wiki's template history. Normalize to the text form either way."""
    raw = raw.strip()
    if raw in NUMERIC_TIER:
        return NUMERIC_TIER[raw]
    if raw and "-Tier" not in raw and "Tier" not in raw:
        return raw + "-Tier"
    return raw


def clean_field(value):
    """Strip HTML/wiki comments (<!-- ... -->) that sometimes trail a field
    value inline, e.g. '|sdate=2023-02-20<!-- estimation -->'."""
    return re.sub(r"<!--.*?-->", "", value).strip()


def resolve_civ(code):
    if not code:
        return None
    code = code.strip().lower()
    if code in ("random", "ran", ""):
        return "Random"
    return CIV_LOOKUP.get(code, code.title())


def load_map_types():
    raw = json.loads((DATA_DIR / "map_types.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_") and k != "types"}


def extract_template(text, start_idx):
    """Given text[start_idx:start_idx+2] == '{{', return the full balanced
    {{...}} block (templates can nest, e.g. Match containing SoloOpponent)."""
    depth = 0
    i = start_idx
    n = len(text)
    while i < n - 1:
        two = text[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
        elif two == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                return text[start_idx:i]
        else:
            i += 1
    return None


def find_templates(text, name):
    pattern = re.compile(r"\{\{" + re.escape(name) + r"(?=[|}\s\n])")
    out = []
    for m in pattern.finditer(text):
        block = extract_template(text, m.start())
        if block:
            out.append(block)
    return out


def find_match_blocks_with_round(text):
    """Like find_templates(text, 'Match') but also returns the round number
    encoded in the preceding bracket key, e.g. '|R3M2={{Match ...}}' -> 3.
    Bracket key is absent for some formats (round stays 0 in that case)."""
    pattern = re.compile(r"(?:\|(R(\d+)M\d+)=)?\{\{Match(?=[|}\s\n])")
    out = []
    for m in pattern.finditer(text):
        match_start = m.end() - len("{{Match")
        block = extract_template(text, match_start)
        if not block:
            continue
        round_num = int(m.group(2)) if m.group(2) else 0
        out.append((round_num, block))
    return out


def parse_template_kv(block, name):
    """{{Name|k1=v1|k2=v2}} -> {'k1': 'v1', 'k2': 'v2', '_pos': [...]}.
    Positional (non key=value) params go in '_pos' in order - e.g.
    {{SoloOpponent|Hera}} is the shorthand form for {{SoloOpponent|name=Hera}},
    used interchangeably with the explicit name=/score=/win= form across
    different tournament pages/eras."""
    inner = re.match(r"\{\{" + re.escape(name) + r"\|?(.*)\}\}$", block, re.S)
    if not inner:
        return {}
    kv = {"_pos": []}
    for part in inner.group(1).split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k.strip()] = v.strip()
        elif part.strip():
            kv["_pos"].append(part.strip())
    return kv


def opponent_name(kv):
    return kv.get("name") or (kv["_pos"][0] if kv.get("_pos") else "")


def infobox_field(region, field):
    m = re.search(r"\|\s*" + re.escape(field) + r"\s*=\s*([^\n|]*)", region)
    return clean_field(m.group(1)) if m else ""


def parse_tournament(title, wikitext):
    head_end = wikitext.find("\n==")
    head = wikitext[:head_end] if head_end != -1 else wikitext[:4000]

    game = infobox_field(head, "game")
    if game and game.lower() not in ("aoe2", "age of empires ii"):
        return None, []

    tier = normalize_tier(infobox_field(head, "liquipediatier") or (TIERS[0] if TIERS else ""))
    name = infobox_field(head, "name") or title
    sdate = infobox_field(head, "sdate")
    edate = infobox_field(head, "edate") or sdate
    organizer = infobox_field(head, "organizer")
    prize = infobox_field(head, "prizepoolusd")
    fmt = infobox_field(head, "format")
    player_number = infobox_field(head, "player_number")
    country = infobox_field(head, "country")
    online_lan = infobox_field(head, "type")

    # missing/unparseable sdate is treated as out of scope rather than
    # in scope - several legacy (pre-2010s) pages don't populate sdate at
    # all under this Infobox format, and silently including them would
    # pollute a "since SINCE_DATE" dataset with undated ancient events.
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", sdate) or sdate < SINCE_DATE:
        return None, []

    map_types = load_map_types()
    games = []
    max_round_seen = -1
    finals = []  # (round, p1, p2, winner)

    for round_num, match_block in find_match_blocks_with_round(wikitext):
        opponents = find_templates(match_block, "SoloOpponent")
        if len(opponents) != 2:
            continue  # skip team / bye / malformed matches
        opp_kv = [parse_template_kv(o, "SoloOpponent") for o in opponents]
        p1, p2 = opponent_name(opp_kv[0]), opponent_name(opp_kv[1])
        if not p1 or not p2:
            continue

        map_blocks = find_templates(match_block, "Map")
        stripped = match_block
        for sub in opponents + map_blocks:
            stripped = stripped.replace(sub, "", 1)
        match_date = infobox_field(stripped, "date")

        # Per-map winner is the only reliably-populated result field across
        # tournament pages/eras - top-level |finished= and |winner= are often
        # left blank even on long-completed matches (e.g. Hidden Cup V), and
        # winner values other than '1'/'2' (e.g. 'skip' for an unplayed map
        # in a shortened bo7) mean "not decided", not a third outcome.
        p1_map_wins = p2_map_wins = 0
        for mb in map_blocks:
            mkv = parse_template_kv(mb, "Map")
            map_name = mkv.get("map", "").strip()
            if not map_name:
                continue
            winner = mkv.get("winner", "").strip()
            if winner not in ("1", "2"):
                winner = None
            elif winner == "1":
                p1_map_wins += 1
            else:
                p2_map_wins += 1
            games.append({
                "tournament": name,
                "tier": tier,
                "date": match_date or sdate,
                "round": round_num,
                "player1": p1,
                "player2": p2,
                "civ1": resolve_civ(mkv.get("civs1")),
                "civ2": resolve_civ(mkv.get("civs2")),
                "map": map_name,
                "mapType": map_types.get(map_name),
                "winner": winner,
            })

        if p1_map_wins != p2_map_wins:  # match decided, inferred from map tally
            match_winner = "1" if p1_map_wins > p2_map_wins else "2"
            if round_num >= max_round_seen:
                max_round_seen = round_num
                finals.append((round_num, p1, p2, match_winner))

    first = second = third = ""
    if finals:
        finals.sort(key=lambda f: f[0])
        _, p1, p2, w = finals[-1]
        first = p1 if w == "1" else p2
        second = p2 if w == "1" else p1
        # best-effort only: no reliable 3rd-place detection without deeper
        # bracket-shape parsing, left blank rather than guessed.

    tournament = {
        "id": title,
        "name": name,
        "url": "https://liquipedia.net/ageofempires/" + title.replace(" ", "_"),
        "tier": tier,
        "start": sdate,
        "end": edate,
        "prize": int(prize) if prize.isdigit() else None,
        "currency": "USD",
        "organizer": organizer,
        "format": fmt,
        "type": online_lan,
        "region": country,
        "players": int(player_number) if player_number.isdigit() else None,
        "first": first,
        "second": second,
        "third": third,
    }
    return tournament, games


CACHE_PATH = DATA_DIR / "_page_cache.json"

# Adding A/B/C-Tier brings the candidate pool to ~1,400+ pages; at one
# action=parse call per 30s that's many hours - more than a single GitHub
# Actions job is allowed to run (6h hard limit). Cap how many pages get
# fetched per invocation; the cache below makes this safe to stop and
# resume across runs without losing progress or redoing work.
MAX_PAGES_PER_RUN = 500


def load_cache():
    """page title -> {"tournament": {...}|None, "games": [...]}. A page
    whose tournament has clearly ended (>3 days ago) is treated as
    permanent and never re-fetched again - completed results don't change.
    Out-of-scope pages (tournament is None) are always permanent under the
    current SINCE_DATE. Invalidated automatically if SINCE_DATE changes."""
    if not CACHE_PATH.exists():
        return {}
    try:
        raw = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if raw.get("since_date") != SINCE_DATE:
        return {}
    return raw.get("pages", {})


def save_cache(cache):
    CACHE_PATH.write_text(
        json.dumps({"since_date": SINCE_DATE, "pages": cache}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def is_permanent(entry):
    if entry.get("tournament") is None:
        return True
    end = (entry["tournament"].get("end") or entry["tournament"].get("start") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", end):
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    return end < cutoff


def write_outputs(pages, cache):
    tournaments = [cache[p]["tournament"] for p in pages if p in cache and cache[p]["tournament"]]
    all_games = [g for p in pages if p in cache and cache[p]["tournament"] for g in cache[p]["games"]]

    DATA_DIR.mkdir(exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    (DATA_DIR / "tournaments.json").write_text(
        json.dumps({"generatedAt": generated_at, "tournaments": tournaments}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (DATA_DIR / "matches.json").write_text(
        json.dumps({"generatedAt": generated_at, "games": all_games}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return tournaments, all_games


def main():
    print("Discovering tournament pages...")
    pages = discover_tournament_pages()
    cache = load_cache()

    needs_fetch = [p for p in pages if p not in cache or not is_permanent(cache[p])]
    never_seen = [p for p in needs_fetch if p not in cache]
    stale = [p for p in needs_fetch if p in cache]
    todo = (never_seen + stale)[:MAX_PAGES_PER_RUN]  # discover new tournaments before refreshing known ones

    print(f"{len(pages)} candidate page(s), {len(cache)} cached (permanent results skipped), "
          f"{len(needs_fetch)} need fetching ({len(never_seen)} new, {len(stale)} refresh) - "
          f"processing {len(todo)} this run (cap={MAX_PAGES_PER_RUN}).")

    unclassified_maps = set()
    for i, page in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] Fetching {page} ...")
        wikitext = api_parse_wikitext(page)
        if wikitext is None:
            print("  -> could not fetch, will retry next run")
            continue
        tournament, games = parse_tournament(page, wikitext)
        cache[page] = {"tournament": tournament, "games": games}
        if tournament is None:
            print(f"  -> out of scope (wrong game or before {SINCE_DATE})")
        else:
            for g in games:
                if g["mapType"] is None:
                    unclassified_maps.add(g["map"])
            print(f"  -> {tournament['name']}: {len(games)} game(s)")
        if i % 5 == 0 or i == len(todo):
            save_cache(cache)  # checkpoint - safe to interrupt/timeout at any point

    if unclassified_maps:
        print("\nMaps with no entry in data/map_types.json (mapType will be null):")
        for m in sorted(unclassified_maps):
            print(f"  - {m}")

    tournaments, all_games = write_outputs(pages, cache)
    print(f"\nWrote {len(tournaments)} tournament(s) and {len(all_games)} game(s).")

    remaining = len(needs_fetch) - len(todo)
    if remaining > 0:
        print(f"{remaining} page(s) still need fetching - the next run (scheduled or manual) will continue from here.")


if __name__ == "__main__":
    sys.exit(main())
