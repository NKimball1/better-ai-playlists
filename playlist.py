"""CLI: natural language -> validated playlist.

Usage:
  python playlist.py "90 min workout playlist, only songs I've liked, no Drake"
  python playlist.py "..." --create      # also create it on Spotify (private)
"""
import json
import sys
from pathlib import Path

from src.agent import run_agent
from src.compiler import compile_spec
from src.library import get_tracks
from src.spec import SourceMode

RUNS = Path(__file__).resolve().parent / "data" / "runs"


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    create = "--create" in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)
    prompt = args[0]

    print("Compiling constraints...")
    spec = compile_spec(prompt)
    print(json.dumps(spec.model_dump(mode="json"), indent=1))

    sp = None
    if spec.hard.source != SourceMode.LIKED_ONLY or create:
        from src.spotify_client import SpotifyClient
        sp = SpotifyClient()

    print("\nRunning agent...")
    run = run_agent(spec, spotify_client=sp)

    print(f"\noutcome={run.outcome} tool_calls={len(run.tool_calls)} "
          f"finalize_attempts={run.finalize_attempts} "
          f"tokens={run.usage} elapsed={run.elapsed_s}s")

    if not run.final_track_ids:
        print("No valid playlist produced.")
        sys.exit(2)

    meta = get_tracks(run.final_track_ids)
    total_min = sum(m["duration_ms"] for m in meta.values()) / 60000
    print(f"\n=== {spec.title} ({len(run.final_track_ids)} tracks, {total_min:.0f} min) ===")
    for i, tid in enumerate(run.final_track_ids, 1):
        m = meta.get(tid)
        if m:
            artists = ", ".join(m["artist_list"])
            print(f"{i:3d}. {m['name']} - {artists} ({m['year']})")
        else:
            print(f"{i:3d}. [external] {tid}")
    if run.ordering_rationale:
        print(f"\nOrdering: {run.ordering_rationale}")

    # persist the trace for the eval layer
    RUNS.mkdir(exist_ok=True)
    trace_file = RUNS / f"run_{abs(hash(prompt)) % 10**8}.json"
    trace_file.write_text(json.dumps({
        "prompt": prompt, "spec": spec.model_dump(mode="json"), **run.to_dict(),
    }, indent=1), encoding="utf-8")
    print(f"\ntrace: {trace_file}")

    if create:
        me = sp.get("/me")
        pl = sp.post("/me/playlists", {
            "name": spec.title, "public": False,
            "description": f"Built by playlist-agent from: {prompt[:120]}"})
        uris = [f"spotify:track:{t}" for t in run.final_track_ids]
        for i in range(0, len(uris), 100):
            sp.post(f"/playlists/{pl['id']}/items", {"uris": uris[i:i+100]})
        print(f"Created on Spotify: {pl.get('external_urls', {}).get('spotify', pl['id'])}")


if __name__ == "__main__":
    main()
