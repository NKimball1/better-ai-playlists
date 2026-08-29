"""The playlist agent: a bounded, tool-gated agentic loop.

Design decisions (deliberate, for discussion):
- Manual loop, not the SDK tool runner: we own budgeting, tool gating,
  trace logging, and the validator-repair cycle.
- Tool availability is POLICY, not prompt: in liked_only mode the
  search_spotify tool is not in the tools list at all.
- The agent cannot emit a playlist directly. finalize_playlist runs the
  deterministic validator; violations come back as an error tool_result
  with repair instructions. The loop only ends on a clean validation or
  when budgets run out.
"""
import json
import os
import time

import anthropic
from dotenv import load_dotenv

from .library import all_tags, search_library
from .spec import PlaylistSpec, SourceMode
from .validator import validate

load_dotenv()

# Haiku 4.5 by default: the deterministic validator catches selection
# mistakes, so the loop tolerates a cheaper model - measured, not assumed
# (see eval results). Override with AGENT_MODEL=claude-opus-5 for the
# quality-ceiling comparison.
MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-4-5")
BASE_TOOL_CALLS = 30       # tool-call budget for playlists up to ~30 tracks
MAX_FINALIZE_ATTEMPTS = 4  # validator repair rounds


def _tool_budget(spec: PlaylistSpec) -> int:
    """Scale the budget with requested playlist size: assembling 60 tracks
    legitimately takes more probing and repair than assembling 15."""
    tracks = spec.hard.track_count or 0
    if spec.hard.target_duration_min:
        tracks = max(tracks, round(spec.hard.target_duration_min / 3.5))
    return BASE_TOOL_CALLS + max(0, tracks - 30)


def _finalize_budget(spec: PlaylistSpec) -> int:
    """Repair rounds also scale with size: each round on a 60-track list
    juggles far more state than on a 15-track list."""
    tracks = spec.hard.track_count or 0
    if spec.hard.target_duration_min:
        tracks = max(tracks, round(spec.hard.target_duration_min / 3.5))
    return MAX_FINALIZE_ATTEMPTS + max(0, tracks // 25)


def _fmt_tracks(rows: list[dict]) -> str:
    """Compact one-line-per-track format to keep token cost down."""
    if not rows:
        return "(no results)"
    out = []
    for r in rows:
        dur = round(r["duration_ms"] / 1000)
        tags = (r.get("tags") or "")[:60]
        out.append(f"{r['id']} | {r['name']} | {r['artists']} | {r.get('year')} | {dur}s | {tags}")
    return "\n".join(out)


def _library_overview() -> str:
    tags = all_tags(min_tracks=15)
    return ("Library tag coverage (tag: n tracks): "
            + ", ".join(f"{t}: {n}" for t, n in tags[:40]))


def _tool_defs(source: SourceMode) -> list[dict]:
    tools = [
        {
            "name": "search_library",
            "description": (
                "Search the user's saved (liked) tracks. Use `query` for free-text "
                "match on track/artist/album NAMES (a query like 'trap' matches song "
                "titles containing the word, not the genre). Use `tag` to filter by "
                "GENRE tag - see the library tag coverage list in your instructions "
                "for valid tags. Combine both, or pass only filters to browse. "
                "Results: id | name | artists | year | duration | tags."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "tag": {"type": "string"},
                    "year_min": {"type": "integer"},
                    "year_max": {"type": "integer"},
                    "limit": {"type": "integer", "maximum": 40},
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "name": "playlist_stats",
            "description": (
                "Free dry-run check of a candidate track list: total duration, "
                "per-artist counts, year range, duplicates. Use this to verify "
                "duration/count math BEFORE finalize_playlist - never sum "
                "durations in your head."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"track_ids": {"type": "array", "items": {"type": "string"}}},
                "required": ["track_ids"],
                "additionalProperties": False,
            },
        },
        {
            "name": "report_infeasible",
            "description": (
                "Declare the request impossible to satisfy from the available "
                "corpus. ONLY valid when your searches PROVE fewer matching "
                "tracks exist than the spec requires (e.g. 'spec needs 50 "
                "tracks from 1995; library contains 1') - evidence must cite "
                "counts from actual search results. 'Assembly is difficult' or "
                "'repair attempts keep failing' is NOT infeasibility; a large "
                "library almost always satisfies size/duration specs. Misusing "
                "this counts as a failure. This ends the task."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["reason", "evidence"],
                "additionalProperties": False,
            },
        },
        {
            "name": "finalize_playlist",
            "description": (
                "Submit the final ordered track id list. The deterministic validator "
                "checks every hard constraint; violations are returned with repair "
                "instructions. Only a clean validation completes the task. Order the "
                "ids to honor the requested energy arc / flow."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "track_ids": {"type": "array", "items": {"type": "string"}},
                    "ordering_rationale": {"type": "string"},
                },
                "required": ["track_ids"],
                "additionalProperties": False,
            },
        },
    ]
    if source != SourceMode.LIKED_ONLY:
        tools.insert(1, {
            "name": "search_spotify",
            "description": (
                "Search the Spotify catalog for tracks OUTSIDE the user's library "
                "(max 10 results per call - be deliberate with queries). "
                f"{'Only use for a minority of tracks; the playlist should lean on the library.' if source == SourceMode.LIBRARY_ADJACENT else ''}"
            ),
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        })
    return tools


SYSTEM_TEMPLATE = """You are a playlist builder operating over a user's real Spotify library.

You will receive a PlaylistSpec. HARD constraints are enforced by a deterministic \
validator when you call finalize_playlist - you cannot bend them. SOFT intent \
(vibe, genres, energy arc) is your quality bar: satisfy it through what you pick \
and how you order it.

{overview}

Method:
1. Probe the library with a few searches (tags for genre, query for names/artists).
2. Build a candidate pool larger than you need, then select for the vibe and \
constraints.
3. Check your candidate list with playlist_stats BEFORE finalizing - it does the \
duration/count/artist-cap math for you. Adjust until stats fit the spec.
4. Order tracks for flow (honor energy_arc if given), then call finalize_playlist.
5. If the validator returns violations, fix exactly what it names - swap or trim \
tracks - and finalize again promptly. Do not start over, and do not keep \
polishing with playlist_stats once the stats fit the spec: finalize.
If searching proves the spec cannot be satisfied from the corpus (e.g. it demands \
more matching tracks than exist), call report_infeasible with the evidence rather \
than exhausting your budget.

Budget: you have {budget} tool calls total. Searches are cheap early, expensive \
late - keep roughly a third of your budget for the finalize/repair cycle.

The spec's soft.notes and ambiguities may contain nuance the fields could not \
capture - honor it where possible."""


class AgentRun:
    """Holds the trace of one run for logging/eval."""

    def __init__(self):
        self.tool_calls: list[dict] = []
        self.finalize_attempts = 0
        self.violations_history: list[list[dict]] = []
        self.final_track_ids: list[str] | None = None
        self.ordering_rationale: str = ""
        self.infeasible_reason: str | None = None
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.elapsed_s = 0.0
        self.outcome = "incomplete"  # clean | budget_exhausted | incomplete

    def to_dict(self):
        return self.__dict__.copy()


def run_agent(spec: PlaylistSpec, spotify_client=None, verbose: bool = True) -> AgentRun:
    """Execute the agent loop. spotify_client only needed outside liked_only."""
    client = anthropic.Anthropic()
    run = AgentRun()
    t0 = time.time()

    # cache of non-library track metadata from spotify search, for the validator
    external_meta: dict[str, dict] = {}

    def exec_tool(name: str, args: dict) -> tuple[str, bool]:
        """Returns (result_text, is_error)."""
        if name == "search_library":
            rows = search_library(
                query=args.get("query", ""), tag=args.get("tag"),
                year_min=args.get("year_min"), year_max=args.get("year_max"),
                exclude_artists=spec.hard.exclude_artists,
                limit=min(int(args.get("limit", 20)), 40))
            return _fmt_tracks(rows), False

        if name == "search_spotify":
            if spotify_client is None:
                return "search_spotify unavailable in this run", True
            resp = spotify_client.get("/search", {
                "q": args["query"], "type": "track", "limit": 10})
            rows = []
            for t in resp.get("tracks", {}).get("items", []):
                year = t["album"].get("release_date", "")[:4]
                row = {
                    "id": t["id"], "name": t["name"],
                    "artists": " ".join(a["name"] for a in t["artists"]),
                    "year": int(year) if year.isdigit() else None,
                    "duration_ms": t["duration_ms"], "tags": "",
                }
                rows.append(row)
                external_meta[t["id"]] = {
                    "id": t["id"], "name": t["name"], "year": row["year"],
                    "duration_ms": t["duration_ms"],
                    "artist_list": [a["name"] for a in t["artists"]],
                }
            return _fmt_tracks(rows), False

        if name == "playlist_stats":
            from .library import get_tracks
            ids = list(args.get("track_ids", []))
            meta = {**get_tracks(ids), **{k: v for k, v in external_meta.items() if k in ids}}
            known = [meta[t] for t in ids if t in meta]
            total = sum(m["duration_ms"] for m in known) / 60000
            primaries: dict[str, int] = {}
            for m in known:
                if m.get("artist_list"):
                    primaries[m["artist_list"][0]] = primaries.get(m["artist_list"][0], 0) + 1
            years = [m["year"] for m in known if m.get("year")]
            dupes = len(ids) - len(set(ids))
            top = sorted(primaries.items(), key=lambda x: -x[1])[:8]
            return (f"tracks: {len(ids)} ({len(ids) - len(known)} unknown ids), "
                    f"total: {total:.1f} min, duplicates: {dupes}, "
                    f"years: {min(years) if years else '?'}-{max(years) if years else '?'}, "
                    f"top primary artists: "
                    + ", ".join(f"{a}x{n}" for a, n in top)), False

        if name == "report_infeasible":
            run.infeasible_reason = f"{args['reason']} | evidence: {args['evidence']}"
            return "Infeasibility recorded. Task ended.", False

        if name == "finalize_playlist":
            run.finalize_attempts += 1
            ids = list(args.get("track_ids", []))
            violations = validate(spec, ids, external_meta)
            run.violations_history.append([v.to_dict() for v in violations])
            if violations:
                if run.finalize_attempts >= finalize_budget:
                    return ("VALIDATION FAILED (final attempt used):\n"
                            + "\n".join(f"- [{v.constraint}] {v.message}" for v in violations)), True
                return ("VALIDATION FAILED - fix these and finalize again:\n"
                        + "\n".join(f"- [{v.constraint}] {v.message}" for v in violations)), True
            run.final_track_ids = ids
            run.ordering_rationale = args.get("ordering_rationale", "")
            return "VALIDATION PASSED. Playlist accepted.", False

        return f"unknown tool {name}", True

    budget = _tool_budget(spec)
    finalize_budget = _finalize_budget(spec)
    tools = _tool_defs(spec.hard.source)
    system = SYSTEM_TEMPLATE.format(overview=_library_overview(), budget=budget)
    messages = [{"role": "user", "content":
                 "PlaylistSpec:\n" + json.dumps(spec.model_dump(mode="json"), indent=1)}]

    while True:
        response = client.messages.create(
            model=MODEL, max_tokens=16000, system=system,
            tools=tools, messages=messages,
            # auto-cache the conversation prefix: every turn after the first
            # re-reads prior turns at ~10% of input price
            cache_control={"type": "ephemeral"})
        run.usage["input_tokens"] += response.usage.input_tokens
        run.usage["output_tokens"] += response.usage.output_tokens
        run.usage["cache_read"] = run.usage.get("cache_read", 0) + \
            (response.usage.cache_read_input_tokens or 0)
        run.usage["cache_write"] = run.usage.get("cache_write", 0) + \
            (response.usage.cache_creation_input_tokens or 0)

        if response.stop_reason != "tool_use":
            break  # model stopped without finalizing (or after success text)

        messages.append({"role": "assistant", "content": response.content})
        results = []
        done = False
        for block in response.content:
            if block.type != "tool_use":
                continue
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            out, is_err = exec_tool(block.name, args)
            run.tool_calls.append({"tool": block.name, "args": args,
                                   "error": is_err, "result_chars": len(out)})
            if verbose:
                arg_s = json.dumps(args)[:80]
                print(f"  [{len(run.tool_calls)}] {block.name}({arg_s}) "
                      f"{'ERR' if is_err else 'ok'}")
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": out, **({"is_error": True} if is_err else {})})
            if block.name == "finalize_playlist" and not is_err and run.final_track_ids:
                done = True
            if block.name == "report_infeasible":
                done = True
        messages.append({"role": "user", "content": results})

        if done:
            run.outcome = "infeasible" if run.infeasible_reason else "clean"
            break
        if len(run.tool_calls) >= budget or \
                run.finalize_attempts >= finalize_budget:
            run.outcome = "budget_exhausted"
            break

    run.elapsed_s = round(time.time() - t0, 1)
    return run
