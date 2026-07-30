# AoE2 Tournament Auto Scout

A tournament tracker + player scouting tool for Age of Empires II pro tournaments, built on data pulled from Liquipedia. Same visual design system as [Auto Scout](https://github.com/robbyho-aoe2/player-data).

Live at: https://robbyho-aoe2.github.io/aoe2-Tournament-Auto-scout/

## How the data pipeline works

`scripts/fetch_liquipedia_data.py` pulls tournament + match data straight from Liquipedia's public MediaWiki API (`action=query` for page discovery, `action=parse` for wikitext) and parses the `{{Match ... {{SoloOpponent...}} ... {{Map|civs1=..|civs2=..|map=..}} }}` bracket templates directly out of the wikitext. Liquipedia's old `action=askargs`/SMW API is gone, and the new structured LPDB API's free tier is currently paused (Enterprise-only) — this wikitext-parsing approach is what still works without an API key.

`.github/workflows/update-data.yml` runs the script on a weekly schedule (and via manual "Run workflow" in the Actions tab) and commits the refreshed `data/tournaments.json` / `data/matches.json`. The page itself just does `fetch('./data/tournaments.json')` — same-origin, no CORS, no manual steps.

### Widening scope

Everything is scoped by two constants at the top of `scripts/fetch_liquipedia_data.py`:

```python
TIERS = ["S-Tier", "A-Tier", "B-Tier", "C-Tier"]
SINCE_DATE = "2023-01-01"
```

Drop tiers you don't want or push `SINCE_DATE` back to pull in more history. Changing either invalidates `data/_page_cache.json` automatically (it re-scans everything once, then caches again).

### `data/_page_cache.json`

Every fetched page is cached here keyed by page title. Once a tournament's end date is more than 3 days in the past, its entry is treated as **permanent** and never re-fetched again — completed results don't change, so there's no reason to keep spending a 30s `action=parse` call on it every run. Only genuinely new pages and still-in-progress/upcoming tournaments get re-fetched on subsequent runs. Out-of-scope pages (wrong game, or before `SINCE_DATE`) are permanent too. Safe to delete if you ever want a full clean re-scan.

With all four tiers enabled, the candidate pool is ~1,400+ pages — at one page per 30s that's several hours, more than a single GitHub Actions job is allowed to run (6h cap). `MAX_PAGES_PER_RUN` in the script (default 500) caps how much a single invocation fetches, checkpointing the cache every 5 pages so a run can be safely interrupted or time out without losing progress. The very first backfill after widening scope will take multiple runs to fully catch up — either wait for the weekly schedule, or manually re-run the Action (Actions tab → "Update tournament data" → Run workflow) a few times in a row to speed it up. After that initial catch-up, weekly runs stay fast since almost everything is permanently cached.

### `data/map_types.json`

Liquipedia doesn't categorize maps the way this project does (closed / semi-open / open / chaotic / hybrid / water / nomad — a scheme borrowed from community "S-tier dominance" style analysis videos, not from Liquipedia itself). This file is a hand-curated lookup and will be incomplete for less common tournament-pool maps — the script prints any unclassified map it encounters so you can add it. Not every AoE2 player will agree on every classification here; edit freely.

### Running it locally

```
python scripts/fetch_liquipedia_data.py
```

No dependencies — stdlib only. Respects the same rate limits as the Action, so a full backfill takes a while the first time; subsequent runs are fast once `_skip_cache.json` is populated.
