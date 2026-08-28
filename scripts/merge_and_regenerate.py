"""Used by the GitHub Action's commit step when a push is rejected because
another run (a manual trigger, an overlapping scheduled run, or a plain
code push) landed on main first. Run *after* `git fetch origin main` and
`git reset --soft origin/main` - merges origin's _page_cache.json/
_name_cache.json with whatever is on disk from this run's own fetch
(local wins on any overlapping key, since this runs right after that
fetch) rather than discarding either side's progress, then regenerates
tournaments.json/matches.json fresh from the merged state. Trying to
resolve a git conflict on the derived tournaments.json/matches.json
directly isn't viable - a full regen changes nearly every line, so two
independent regenerations conflict on almost the whole file.
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_liquipedia_data as f


def merge_json_file(path, is_wrapped):
    origin_raw = subprocess.run(
        ["git", "show", f"origin/main:{path}"],
        capture_output=True, text=True, check=True,
    ).stdout
    origin_data = json.loads(origin_raw)
    local_data = json.loads(Path(path).read_text(encoding="utf-8"))
    if is_wrapped:
        origin_pages = origin_data.get("pages", {})
        local_pages = local_data.get("pages", {})
        merged = {"since_date": local_data.get("since_date"), "pages": {**origin_pages, **local_pages}}
    else:
        merged = {**origin_data, **local_data}
    Path(path).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False, sort_keys=not is_wrapped),
        encoding="utf-8",
    )


merge_json_file("data/_page_cache.json", is_wrapped=True)
merge_json_file("data/_name_cache.json", is_wrapped=False)

cache = f.load_cache()
tournaments, all_games = f.write_outputs(list(cache.keys()), cache)
print(f"Merged with origin/main and regenerated: {len(tournaments)} tournament(s), {len(all_games)} game(s).")
