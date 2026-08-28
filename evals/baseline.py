"""Naive baseline: one LLM call, no tools, no validator, no library access.

This approximates how a prompt-to-playlist feature behaves without the
architecture this project argues for: the model names songs from world
knowledge, and we then check how the result scores against the same spec
the agent was held to. Song names are resolved to the user's library via
exact-ish FTS match; unresolvable tracks count as source violations in
liked_only mode (the model picked music the user never saved).
"""
import os

import anthropic
from dotenv import load_dotenv

from src.library import search_library
from src.spec import PlaylistSpec

load_dotenv()

MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-4-5")

BASELINE_SYSTEM = """You create playlists. Given a request, reply with ONLY a \
numbered list of tracks, one per line, formatted exactly as:
1. Song Title - Artist Name
Pick real songs that fit the request. You do not have access to the user's \
listening history or library; if the request references their liked/saved \
songs, make your best guesses at well-known songs matching the request's vibe \
anyway. ALWAYS produce the full list - never decline, never ask questions, \
never add commentary."""


def run_baseline(prompt: str, spec: PlaylistSpec) -> dict:
    """Returns {track_ids, unresolved, raw_lines}. Resolution: best FTS match."""
    client = anthropic.Anthropic()
    n = spec.hard.track_count or 20
    response = client.messages.create(
        model=MODEL, max_tokens=4000, system=BASELINE_SYSTEM,
        messages=[{"role": "user", "content": f"{prompt}\n\n({n} tracks)"}])
    text = next(b.text for b in response.content if b.type == "text")

    track_ids, unresolved, raw = [], [], []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or "." not in line[:4]:
            continue
        entry = line.split(".", 1)[1].strip()
        if " - " not in entry or entry.endswith("?"):
            continue  # not a track line (commentary/questions)
        raw.append(entry)
        title = entry.split(" - ")[0].strip()
        artist = entry.split(" - ")[1].strip() if " - " in entry else ""
        hits = search_library(query=f"{title} {artist}", limit=1)
        if not hits:
            hits = search_library(query=title, limit=1)
        # accept only if the title actually matches (FTS is fuzzy)
        if hits and title.lower() in hits[0]["name"].lower():
            track_ids.append(hits[0]["id"])
        else:
            unresolved.append(entry)

    usage = {"input_tokens": response.usage.input_tokens,
             "output_tokens": response.usage.output_tokens}
    return {"track_ids": track_ids, "unresolved": unresolved,
            "raw_lines": raw, "usage": usage}
