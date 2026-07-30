# AoE2 Tournament Auto Scout

A tournament tracker + player scouting tool for Age of Empires II pro tournaments, built on data pulled from Liquipedia. Same visual design system as [Auto Scout](https://github.com/robbyho-aoe2/player-data).

Live at: https://robbyho-aoe2.github.io/aoe2-Tournament-Auto-scout/

## How the data pipeline works

`scripts/fetch_liquipedia_data.py` pulls tournament + match data straight from Liquipedia's public MediaWiki API (`action=query` for page discovery, `action=parse` for wikitext) and parses the `{{Match ... {{SoloOpponent...}} ... {{Map|civs1=..|civs2=..|map=..}} }}` bracket templates directly out of the wikitext. Liquipedia's old `action=askargs`/SMW API is gone, and the new structured LPDB API's free tier is currently paused (Enterprise-only) — this wikitext-parsing approach is what still works without an API key.

`.github/workflows/update-data.yml` runs the script on a weekly schedule (and via manual "Run workflow" in the Actions tab) and commits the refreshed `data/tournaments.json` / `data/matches.json`. The page itself just does `fetch('./data/tournaments.json')` — same-origin, no CORS, no manual steps.

### Widening scope

Everything is scoped by two constants at the top of `scripts/fetch_liquipedia_data.py`:

```python
TIERS = ["S-Tier"]
SINCE_DATE = "2023-01-01"
```

Add `"A-Tier"` etc. to `TIERS` or push `SINCE_DATE` back to pull in more history. Changing either invalidates `data/_skip_cache.json` automatically (it re-scans everything once, then caches again).

### `data/_skip_cache.json`

`Category:S-Tier Tournaments` spans the AoE2 wiki's entire ~25-year history, but Liquipedia's API only allows one `action=parse` call every 30 seconds. Without caching, every run — including every future weekly cron run — would burn ~70 minutes re-fetching and discarding the same ~120 pre-2023 pages. This file remembers "already checked, out of scope" pages so only genuinely new/in-scope tournaments get re-fetched on subsequent runs. Safe to delete if you ever want a full clean re-scan.

### `data/map_types.json`

Liquipedia doesn't categorize maps the way this project does (closed / semi-open / open / chaotic / hybrid / water / nomad — a scheme borrowed from community "S-tier dominance" style analysis videos, not from Liquipedia itself). This file is a hand-curated lookup and will be incomplete for less common tournament-pool maps — the script prints any unclassified map it encounters so you can add it. Not every AoE2 player will agree on every classification here; edit freely.

### Running it locally

```
python scripts/fetch_liquipedia_data.py
```

No dependencies — stdlib only. Respects the same rate limits as the Action, so a full backfill takes a while the first time; subsequent runs are fast once `_skip_cache.json` is populated.
