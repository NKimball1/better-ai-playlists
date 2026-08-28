"""Constraint compiler: natural language -> PlaylistSpec.

One structured-output call. The model's only job is faithful translation;
defaults are conservative (liked_only unless the prompt clearly asks for
discovery).
"""
import anthropic
from dotenv import load_dotenv

load_dotenv()

from .spec import PlaylistSpec

SYSTEM = """You compile a user's playlist request into a typed PlaylistSpec.

Rules:
- hard constraints are things the user stated as requirements (counts, \
durations, year ranges, only-liked-songs, artist inclusions/exclusions). \
If the user says "only songs I've liked/saved/in my library", source is \
liked_only. If they ask for discovery/new music, open_discovery. If they \
want library plus similar artists, library_adjacent. Default: liked_only.
- soft intent is everything aesthetic: mood, genre, energy, era feel.
- Do NOT invent constraints the user didn't state. Leave fields null/empty \
rather than guessing. If part of the request fits neither hard nor soft \
fields, record it in ambiguities verbatim.
- track_count only if they named a number; target_duration_min only if \
they named a length of time."""

_client = None


def compile_spec(prompt: str) -> PlaylistSpec:
    global _client
    if _client is None:
        import os
        headers = {}
        if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
            headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"]
        _client = anthropic.Anthropic(default_headers=headers or None)
    response = _client.messages.parse(
        model="claude-opus-5",
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=PlaylistSpec,
    )
    return response.parsed_output
