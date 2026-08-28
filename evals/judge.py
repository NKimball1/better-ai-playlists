"""Pairwise LLM judge for soft intent.

Scope discipline: hard constraints are measured by the validator, so the
judge is asked ONLY about what code cannot check - vibe fit, coherence,
ordering/flow. Pairwise (A vs B) rather than absolute scores because
LLM judges are far more reliable at comparison than calibration.

Position bias control: presentation order is flipped per call (seeded by
prompt id, so runs are reproducible) and the mapping is recorded.
"""
import os

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-haiku-4-5")

class Verdict(BaseModel):
    winner: str = Field(description="'1', '2', or 'tie'")
    vibe_fit: str = Field(description="which playlist better matches the requested mood/genre: '1', '2', or 'tie'")
    coherence: str = Field(description="which hangs together better as one listening session: '1', '2', or 'tie'")
    rationale: str = Field(description="2-3 sentences, concrete")


JUDGE_SYSTEM = """You judge which of two playlists better satisfies the AESTHETIC \
intent of a request: mood/vibe fit, coherence as a single listening session, and \
ordering/flow. IGNORE mechanical requirements (counts, durations, whether songs \
come from a library) - those are measured elsewhere. Judge only from the track \
lists given. If you don't recognize a track, infer what you can from artist and \
title. Prefer a decisive answer; use 'tie' only when genuinely inseparable."""

_client = None


def judge_pair(prompt: str, playlist_a: list[str], playlist_b: list[str],
               seed: int = 0) -> dict:
    """playlist_x: list of 'Title - Artist (year)' lines, in play order.
    Returns {winner: 'A'|'B'|'tie', vibe_fit, coherence, rationale, a_shown_first}."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic()

    a_first = seed % 2 == 0
    first, second = (playlist_a, playlist_b) if a_first else (playlist_b, playlist_a)
    body = (f"Request: {prompt}\n\nPlaylist 1:\n" + "\n".join(first)
            + "\n\nPlaylist 2:\n" + "\n".join(second))

    response = _client.messages.parse(
        model=JUDGE_MODEL, max_tokens=2000, system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": body}],
        output_format=Verdict)
    v = response.parsed_output

    def unmap(x: str) -> str:
        if x == "tie":
            return "tie"
        return ("A" if x == "1" else "B") if a_first else ("A" if x == "2" else "B")

    return {"winner": unmap(v.winner), "vibe_fit": unmap(v.vibe_fit),
            "coherence": unmap(v.coherence), "rationale": v.rationale,
            "a_shown_first": a_first,
            "usage": {"input_tokens": response.usage.input_tokens,
                      "output_tokens": response.usage.output_tokens}}
