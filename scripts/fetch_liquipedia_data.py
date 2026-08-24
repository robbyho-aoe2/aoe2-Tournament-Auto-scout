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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Tournament/player names routinely include non-ASCII characters (Vietnamese,
# accented Latin, etc). Windows' console defaults to a legacy codepage (cp1252)
# that can't encode most of them, which crashes any print() containing one -
# and since this script normally runs piped through `tee`, that crash's exit
# code gets masked by tee's own (0), making the run look like it succeeded
# when it actually died after a handful of pages. Force UTF-8 stdout so this
# can't happen regardless of the terminal's default encoding.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def _throttled_get(params, interval, last_ts_holder, retries=3):
    elapsed = time.monotonic() - last_ts_holder[0]
    if elapsed < interval:
        time.sleep(interval - elapsed)
    url = API + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        # 429s happen occasionally even with correct client-side throttling
        # (their server-side limiter can trigger on shorter/cumulative
        # windows) - back off and retry rather than losing the whole run
        # over one transient hit. Respects their own Retry-After if given.
        if e.code == 429 and retries > 0:
            wait = int(e.headers.get("Retry-After", 60))
            print(f"  (rate limited, waiting {wait}s before retry...)")
            time.sleep(wait)
            last_ts_holder[0] = time.monotonic()
            return _throttled_get(params, interval, last_ts_holder, retries=retries - 1)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # transient network blips (DNS hiccup, connection timeout, brief
        # drop) shouldn't cost hours of progress either - same retry
        # treatment as a 429, just a fixed shorter backoff since there's no
        # Retry-After to honor here.
        if retries > 0:
            print(f"  (network error: {e}; waiting 30s before retry...)")
            time.sleep(30)
            last_ts_holder[0] = time.monotonic()
            return _throttled_get(params, interval, last_ts_holder, retries=retries - 1)
        raise
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


NAME_CACHE_PATH = DATA_DIR / "_name_cache.json"
_name_cache = None


def load_name_cache():
    if not NAME_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(NAME_CACHE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_name_cache(cache):
    NAME_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )


def resolve_player_name(name):
    """Canonicalize a player name via Liquipedia's own redirect system -
    e.g. the page 'Mr Yo' redirects to 'Yo' (a documented former handle for
    the same pro). Different tournament pages/eras tag the same person
    under whatever name was current at the time, so without this a
    player's record gets silently split across every handle they've ever
    competed under. Cached persistently since most names aren't redirects
    and repeat across many games.

    Also honors MediaWiki's own title normalization (distinct from
    redirects): page titles always force-capitalize their first letter,
    so a wikitext mention like 'ciskhan' queries as the same page as
    'Ciskhan' even with no redirect involved - the API reports this via a
    top-level 'normalized' key, separate from 'redirects'. Skipping it
    previously left lowercase-first-letter spellings uncorrected, quietly
    splitting a handful of players' records across two casings of the
    same name (verified directly against the live API, not guessed).

    Important: MediaWiki normalizes title casing syntactically even when
    no page exists at that title at all (e.g. querying 'abc' normalizes
    to 'Abc' and reports it as 'missing') - most raw names in this data
    are community handles with no Liquipedia page whatsoever, so blindly
    trusting normalization would fabricate a capitalization for them.
    The resolved title is only trusted when the page actually exists."""
    global _name_cache
    if _name_cache is None:
        _name_cache = load_name_cache()
    if not name:
        return name
    if name in _name_cache:
        return _name_cache[name]
    canonical = name
    try:
        data = api_query(titles=name, redirects=1)
        query = data.get("query", {})
        candidate = name
        normalized = query.get("normalized")
        if normalized:
            candidate = normalized[0]["to"]
        redirects = query.get("redirects")
        if redirects:
            candidate = redirects[0]["to"]
        page = next(iter(query.get("pages", {}).values()), None)
        if page is not None and "missing" not in page:
            canonical = candidate
    except Exception:
        canonical = name
    _name_cache[name] = canonical
    save_name_cache(_name_cache)
    return canonical


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
    'hin': 'Hindustanis', 'hun': 'Huns', 'inc': 'Inca', 'ind': 'Indians',
    'ita': 'Italians', 'jap': 'Japanese', 'khm': 'Khmer', 'kor': 'Koreans',
    'lit': 'Lithuanians', 'mag': 'Magyars', 'mly': 'Malay', 'mal': 'Malians',
    'mli': 'Malians', 'may': 'Maya', 'mon': 'Mongols', 'per': 'Persians',
    'pol': 'Poles', 'por': 'Portuguese', 'rom': 'Romans', 'sar': 'Saracens',
    'sic': 'Sicilians', 'sla': 'Slavs', 'slav': 'Slavs', 'spa': 'Spanish',
    'tat': 'Tatars', 'teu': 'Teutons', 'tur': 'Turks', 'vie': 'Vietnamese',
    'viet': 'Vietnamese', 'vik': 'Vikings', 'viking': 'Vikings',
    # Three Kingdoms DLC
    'khi': 'Khitans', 'jur': 'Jurchens', 'wei': 'Wei', 'wu': 'Wu', 'shu': 'Shu',
    # The Last Chieftains DLC
    'map': 'Mapuche', 'mui': 'Muisca', 'tup': 'Tupi',
    # Chronicles: Battle for Greece / Alexander the Great
    'ath': 'Athenians', 'ach': 'Achaemenids', 'spartans': 'Spartans',
    # typos seen on individual tournament pages (single-letter slips against
    # the real code - byz/spa/tat - each only ever seen once so far, kept
    # separate from the confirmed entries above)
    'cmss': 'Cumans', 'biz': 'Byzantines', 'esp': 'Spanish', 'tar': 'Tatars',
}

# Corrections for civ names already baked into cached game rows (as text,
# not codes) - applied at write time so already-fetched data self-heals
# without needing to re-fetch anything. Add here first if the game name
# for a civ turns out to be wrong; only mirror into CIV_LOOKUP above once
# confirmed, since that changes what NEW fetches resolve going forward.
CIV_RENAME = {
    'Incas': 'Inca', 'Mayans': 'Maya', 'Khi': 'Khitans', 'Jur': 'Jurchens',
    'Map': 'Mapuche', 'Mui': 'Muisca', 'Tup': 'Tupi',
    'Ath': 'Athenians', 'Ach': 'Achaemenids',
    'Burgudians': 'Burgundians', 'Viking': 'Vikings', 'Cmss': 'Cumans',
    'Biz': 'Byzantines', 'Esp': 'Spanish', 'Tar': 'Tatars',
    'Azt<!--Azt? Please Re-Check Different Website-->': 'Aztecs',
}


NUMERIC_TIER = {"1": "S-Tier", "2": "A-Tier", "3": "B-Tier", "4": "C-Tier", "5": "D-Tier"}


def normalize_tier(raw):
    """Some tournament pages store |liquipediatier= as a bare number (1=S,
    2=A, ...), and casing of the text form ('C-tier' vs 'C-Tier') isn't
    consistent either across the wiki's template history. Normalize to a
    single canonical 'X-Tier' form."""
    raw = raw.strip()
    if raw in NUMERIC_TIER:
        return NUMERIC_TIER[raw]
    if not raw:
        return raw
    letter = raw[0].upper()
    if letter in "SABCD":
        return f"{letter}-Tier"
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
            # editors sometimes leave inline wiki comments on a field value,
            # e.g. civs1=Azt<!--Azt? Please Re-Check Different Website--> -
            # strip them here so they don't leak into the resolved value.
            kv[k.strip()] = clean_field(v)
        elif part.strip():
            kv["_pos"].append(clean_field(part))
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
        team_opponents = find_templates(match_block, "TeamOpponent")

        if len(opponents) == 2:
            opp_kv = [parse_template_kv(o, "SoloOpponent") for o in opponents]
            p1 = resolve_player_name(opponent_name(opp_kv[0]))
            p2 = resolve_player_name(opponent_name(opp_kv[1]))
            if not p1 or not p2:
                continue
            map_blocks = find_templates(match_block, "Map")
            stripped = match_block
            for sub in opponents + map_blocks:
                stripped = stripped.replace(sub, "", 1)
            match_date = infobox_field(stripped, "date")

            # Per-map winner is the only reliably-populated result field
            # across tournament pages/eras - top-level |finished= and
            # |winner= are often left blank even on long-completed matches
            # (e.g. Hidden Cup V), and winner values other than '1'/'2'
            # (e.g. 'skip' for an unplayed map in a shortened bo7) mean
            # "not decided", not a third outcome.
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
                    "format": "1v1",
                    "player1": p1,
                    "player2": p2,
                    "teammates": [],
                    "team1": None,
                    "team2": None,
                    "civ1": resolve_civ(mkv.get("civs1")),
                    "civ2": resolve_civ(mkv.get("civs2")),
                    "map": map_name,
                    "mapTypes": map_types.get(map_name, []),
                    "winner": winner,
                })

            if p1_map_wins != p2_map_wins:  # match decided, inferred from map tally
                match_winner = "1" if p1_map_wins > p2_map_wins else "2"
                if round_num >= max_round_seen:
                    max_round_seen = round_num
                    finals.append((round_num, p1, p2, match_winner))

        elif len(team_opponents) == 2:
            # Team formats (2v2/3v3/4v4) use {{TeamOpponent|template=<team
            # name>}} instead of {{SoloOpponent}} - individual identities
            # only show up per-map, as comma-separated players1/civs1 lists
            # in {{Map|...}}, since who actually played a given map can
            # vary within a match (substitutions). One row is emitted per
            # (player, map) from each side, tagged format='team' with no
            # single individual opponent - "winner" is always relative to
            # that row's own player1, same convention as 1v1 rows.
            team_kv = [parse_template_kv(o, "TeamOpponent") for o in team_opponents]
            team1 = team_kv[0].get("template") or team_kv[0].get("name") or "Team 1"
            team2 = team_kv[1].get("template") or team_kv[1].get("name") or "Team 2"
            map_blocks = find_templates(match_block, "Map")
            stripped = match_block
            for sub in team_opponents + map_blocks:
                stripped = stripped.replace(sub, "", 1)
            match_date = infobox_field(stripped, "date")

            team1_map_wins = team2_map_wins = 0
            for mb in map_blocks:
                mkv = parse_template_kv(mb, "Map")
                map_name = mkv.get("map", "").strip()
                winner = mkv.get("winner", "").strip()
                if not map_name or winner not in ("1", "2"):
                    continue  # no per-player pairing worth keeping for an undecided map
                if winner == "1":
                    team1_map_wins += 1
                else:
                    team2_map_wins += 1
                players1 = [s.strip() for s in mkv.get("players1", "").split(",") if s.strip()]
                players2 = [s.strip() for s in mkv.get("players2", "").split(",") if s.strip()]
                civs1 = [s.strip() for s in mkv.get("civs1", "").split(",")]
                civs2 = [s.strip() for s in mkv.get("civs2", "").split(",")]
                for side, players, civs, own_team, opp_team in (
                    ("1", players1, civs1, team1, team2),
                    ("2", players2, civs2, team2, team1),
                ):
                    for idx, raw_name in enumerate(players):
                        teammates = [resolve_player_name(p) for j, p in enumerate(players) if j != idx]
                        games.append({
                            "tournament": name,
                            "tier": tier,
                            "date": match_date or sdate,
                            "round": round_num,
                            "format": "team",
                            "player1": resolve_player_name(raw_name),
                            "player2": None,
                            "teammates": teammates,
                            "team1": own_team,
                            "team2": opp_team,
                            "civ1": resolve_civ(civs[idx]) if idx < len(civs) else None,
                            "civ2": None,
                            "map": map_name,
                            "mapTypes": map_types.get(map_name, []),
                            "winner": "1" if winner == side else "2",
                        })

            if team1_map_wins != team2_map_wins:
                match_winner = "1" if team1_map_wins > team2_map_wins else "2"
                if round_num >= max_round_seen:
                    max_round_seen = round_num
                    finals.append((round_num, team1, team2, match_winner))
        else:
            continue  # bye / malformed / unrecognized opponent template

    first = second = third = ""
    if finals:
        finals.sort(key=lambda f: f[0])
        _, p1, p2, w = finals[-1]
        first = p1 if w == "1" else p2
        second = p2 if w == "1" else p1
        # best-effort only: no reliable 3rd-place detection without deeper
        # bracket-shape parsing, left blank rather than guessed.

    formats_seen = {g["format"] for g in games}
    format_type = "mixed" if len(formats_seen) > 1 else (formats_seen.pop() if formats_seen else "unknown")

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
        "formatType": format_type,
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
    # re-resolve names and re-normalize tier here too, not just at parse
    # time - cache entries written before these existed/were fixed still
    # have raw values baked in, and this is the one place all of them pass
    # through before being published, regardless of when they were fetched.
    tournaments = []
    all_games = []
    map_types = load_map_types()  # re-applied here too - editing map_types.json
    # should retroactively reclassify already-cached games, not just new fetches
    for p in pages:
        entry = cache.get(p)
        if not entry or not entry["tournament"]:
            continue
        t = dict(entry["tournament"])
        t["tier"] = normalize_tier(t["tier"])
        for key in ("first", "second", "third"):
            if t.get(key):
                t[key] = resolve_player_name(t[key])
        tournaments.append(t)
        for g in entry["games"]:
            g2 = dict(g)
            g2["player1"] = resolve_player_name(g["player1"])
            g2["player2"] = resolve_player_name(g["player2"]) if g.get("player2") else None
            g2["tier"] = normalize_tier(g["tier"])
            # rows cached before the format/team1/team2 fields existed
            # predate team-match support entirely, so they're always 1v1
            g2.setdefault("format", "1v1" if g.get("player2") else "team")
            g2.setdefault("team1", None)
            g2.setdefault("team2", None)
            g2["teammates"] = [resolve_player_name(t) for t in g.get("teammates", [])]
            if g2.get("civ1") in CIV_RENAME:
                g2["civ1"] = CIV_RENAME[g2["civ1"]]
            if g2.get("civ2") in CIV_RENAME:
                g2["civ2"] = CIV_RENAME[g2["civ2"]]
            g2["mapTypes"] = map_types.get(g2["map"], [])
            all_games.append(g2)

    DATA_DIR.mkdir(exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    (DATA_DIR / "tournaments.json").write_text(
        json.dumps({"generatedAt": generated_at, "sinceDate": SINCE_DATE, "tournaments": tournaments}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (DATA_DIR / "matches.json").write_text(
        json.dumps({"generatedAt": generated_at, "sinceDate": SINCE_DATE, "games": all_games}, indent=2, ensure_ascii=False),
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
                if not g["mapTypes"]:
                    unclassified_maps.add(g["map"])
            print(f"  -> {tournament['name']}: {len(games)} game(s)")
        if i % 5 == 0 or i == len(todo):
            save_cache(cache)  # checkpoint - safe to interrupt/timeout at any point

    if unclassified_maps:
        print("\nMaps with no entry in data/map_types.json (mapTypes will be empty):")
        for m in sorted(unclassified_maps):
            print(f"  - {m}")

    tournaments, all_games = write_outputs(pages, cache)
    print(f"\nWrote {len(tournaments)} tournament(s) and {len(all_games)} game(s).")

    remaining = len(needs_fetch) - len(todo)
    if remaining > 0:
        print(f"{remaining} page(s) still need fetching - the next run (scheduled or manual) will continue from here.")


if __name__ == "__main__":
    sys.exit(main())
