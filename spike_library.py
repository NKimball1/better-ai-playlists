"""Spike: pull the user's Spotify library and report what we have to work with.

Answers the design questions:
  1. How big is the library (tracks, unique artists, albums, year spread)?
  2. Are artist `genres` still populated in dev mode? (determines whether we
     need MusicBrainz enrichment for the retrieval layer)
  3. Do /me/top/* and /me/player/recently-played respond as documented?
Saves a snapshot to data/library.json for the real ingestion step later.
"""
import json
import time
from collections import Counter
from pathlib import Path

from src.spotify_client import SpotifyClient

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)


def main():
    sp = SpotifyClient()

    me = sp.get("/me")
    print(f"Authenticated as: {me.get('display_name')} ({me.get('id')}), "
          f"product={me.get('product')}\n")

    # ---- 1. Saved tracks -------------------------------------------------
    print("Pulling saved tracks (paginated)...")
    tracks = []
    t0 = time.time()
    for item in sp.paginate("/me/tracks", {"limit": 50}):
        t = item.get("track") or {}
        if not t.get("id"):
            continue  # local files / removed tracks
        tracks.append({
            "id": t["id"],
            "name": t["name"],
            "artists": [{"id": a["id"], "name": a["name"]} for a in t["artists"]],
            "album": t["album"]["name"],
            "album_id": t["album"]["id"],
            "release_date": t["album"].get("release_date", ""),
            "duration_ms": t["duration_ms"],
            "popularity": t.get("popularity"),
            "added_at": item.get("added_at"),
        })
        if len(tracks) % 200 == 0:
            print(f"  {len(tracks)} tracks...")
    elapsed = time.time() - t0

    artist_ids = {a["id"] for t in tracks for a in t["artists"] if a["id"]}
    albums = {t["album_id"] for t in tracks}
    years = Counter(t["release_date"][:4] for t in tracks if t["release_date"])
    total_min = sum(t["duration_ms"] for t in tracks) / 60000

    print(f"\n=== LIBRARY ===")
    print(f"Saved tracks:    {len(tracks)}  (pulled in {elapsed:.1f}s)")
    print(f"Unique artists:  {len(artist_ids)}")
    print(f"Unique albums:   {len(albums)}")
    print(f"Total duration:  {total_min:.0f} min")
    if years:
        top_years = ", ".join(f"{y}:{n}" for y, n in years.most_common(8))
        print(f"Top years:       {top_years}")

    # ---- 2. Artist genres: sample individually (no batch endpoint) -------
    print(f"\n=== ARTIST GENRES (sample of 25 / {len(artist_ids)}) ===")
    sample = list(artist_ids)[:25]
    with_genres = 0
    genre_counter = Counter()
    for aid in sample:
        a = sp.get(f"/artists/{aid}")
        g = a.get("genres", [])
        if g:
            with_genres += 1
            genre_counter.update(g)
    print(f"Artists with non-empty genres: {with_genres}/25")
    if genre_counter:
        print(f"Sample genres: {', '.join(g for g, _ in genre_counter.most_common(10))}")
    else:
        print("!! genres empty across sample -> MusicBrainz enrichment REQUIRED")

    # ---- 3. Top items + recently played ---------------------------------
    print(f"\n=== OTHER ENDPOINTS ===")
    for label, path, params in [
        ("top tracks (medium_term)", "/me/top/tracks", {"limit": 10, "time_range": "medium_term"}),
        ("top artists (medium_term)", "/me/top/artists", {"limit": 10, "time_range": "medium_term"}),
        ("recently played", "/me/player/recently-played", {"limit": 20}),
    ]:
        try:
            resp = sp.get(path, params)
            n = len(resp.get("items", []))
            print(f"{label:28s} OK ({n} items, total={resp.get('total', '?')})")
        except Exception as e:
            print(f"{label:28s} FAILED: {e}")

    # top artists come back as full artist objects -> free genre signal
    try:
        top_a = sp.get("/me/top/artists", {"limit": 20, "time_range": "long_term"})
        ga = [a for a in top_a.get("items", []) if a.get("genres")]
        print(f"top artists with genres:     {len(ga)}/{len(top_a.get('items', []))}")
    except Exception as e:
        print(f"top artists genre check failed: {e}")

    # ---- snapshot --------------------------------------------------------
    out = DATA / "library.json"
    out.write_text(json.dumps(tracks, indent=1), encoding="utf-8")
    print(f"\nSnapshot saved: {out} ({len(tracks)} tracks)")


if __name__ == "__main__":
    main()
