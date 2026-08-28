"""Enrich library artists with MusicBrainz genre/tag data.

Spotify dev mode (Feb 2026) strips `genres` and `popularity` from all
responses, so this is the project's only source of "what does this
artist sound like" metadata.

Resumable: writes data/artist_genres.json after every lookup; rerunning
skips artists already fetched. MusicBrainz requires 1 req/sec.
"""
import json
import re
import sys
import time

# Windows consoles default to cp1252; artist names are unicode
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent.parent / "data"
OUT = DATA / "artist_genres.json"
MB = "https://musicbrainz.org/ws/2/artist/"
HEADERS = {"User-Agent": "BetterAIPlaylists/0.1 (personal portfolio project)"}
MIN_SCORE = 85  # MusicBrainz search confidence threshold


def lookup_artist(name: str) -> dict:
    """Search MusicBrainz for an artist; return best match with tags/genres."""
    resp = requests.get(MB, headers=HEADERS, params={
        "query": f'artist:"{name}"', "fmt": "json", "limit": 3,
    })
    if resp.status_code == 503:  # rate-limit backoff
        time.sleep(3)
        return lookup_artist(name)
    resp.raise_for_status()
    hits = resp.json().get("artists", [])
    if not hits:
        # names like "A$AP Ferg" break Lucene; retry sanitized once
        clean = re.sub(r"[$]", "S", name)
        clean = re.sub(r"[^\w\s'&.-]", " ", clean).strip()
        if clean and clean.lower() != name.lower():
            time.sleep(1.1)
            return lookup_artist(clean)
        return {"matched": False, "reason": "no_results"}
    best = hits[0]
    if int(best.get("score", 0)) < MIN_SCORE:
        return {"matched": False, "reason": f"low_score:{best.get('score')}",
                "candidate": best.get("name")}
    tags = sorted(
        (t["name"] for t in best.get("tags", []) if int(t.get("count", 0)) > 0),
        key=str.lower,
    )
    return {
        "matched": True,
        "mb_id": best["id"],
        "mb_name": best["name"],
        "score": int(best.get("score", 0)),
        "tags": tags,
        "type": best.get("type"),
        "disambiguation": best.get("disambiguation", ""),
    }


def main(limit: int | None = None):
    tracks = json.loads((DATA / "library.json").read_text(encoding="utf-8"))
    # order artists by track count so the most common ones enrich first
    counts = {}
    names = {}
    for t in tracks:
        for a in t["artists"]:
            if a["id"]:
                counts[a["id"]] = counts.get(a["id"], 0) + 1
                names[a["id"]] = a["name"]
    ordered = sorted(counts, key=counts.get, reverse=True)

    done = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    todo = [aid for aid in ordered if aid not in done]
    if limit:
        todo = todo[:limit]
    print(f"{len(done)} done, {len(todo)} to fetch")

    for i, aid in enumerate(todo, 1):
        name = names[aid]
        try:
            done[aid] = {"name": name, **lookup_artist(name)}
        except Exception as e:
            done[aid] = {"name": name, "matched": False, "reason": f"error:{e}"}
        OUT.write_text(json.dumps(done, indent=1), encoding="utf-8")
        m = done[aid]
        status = f"[{','.join(m['tags'][:4])}]" if m.get("matched") else f"MISS({m.get('reason')})"
        print(f"{i}/{len(todo)} {name}: {status}", flush=True)
        time.sleep(1.1)

    matched = sum(1 for v in done.values() if v.get("matched"))
    tagged = sum(1 for v in done.values() if v.get("tags"))
    print(f"\nmatched: {matched}/{len(done)}, with tags: {tagged}/{len(done)}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else None)
