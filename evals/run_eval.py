"""Eval runner.

Usage:
  python -m evals.run_eval --smoke            # 5 prompts, agent only  (~$0.25)
  python -m evals.run_eval --smoke --baseline # + naive baseline comparison
  python -m evals.run_eval --full             # all prompts (ask before running)
  python -m evals.run_eval --ids s01,g14      # specific prompts

Measures two things per prompt:
  1. COMPILER: does the compiled spec match the golden expected fields?
  2. AGENT: constraint-satisfaction of the final playlist (validator re-run),
     plus repair-loop stats, tool calls, tokens.
The baseline is scored by the SAME validator against the SAME spec.

Results -> data/eval_runs/<runstamp>/ (one JSON per prompt + summary.json).
"""
import argparse
import json
import sys
import time
from pathlib import Path

from src.compiler import compile_spec
from src.agent import run_agent, MODEL
from src.spec import PlaylistSpec, SourceMode
from src.validator import validate

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = json.loads((ROOT / "evals" / "golden_prompts.json").read_text(encoding="utf-8"))
OUT_BASE = ROOT / "data" / "eval_runs"

# $/M tokens for cost accounting
PRICES = {
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
    "claude-opus-5": (5.0, 25.0, 6.25, 0.50),
}


def cost_of(usage: dict, model: str) -> float:
    inp, out, cw, cr = PRICES.get(model, PRICES["claude-haiku-4-5"])
    return (usage.get("input_tokens", 0) * inp
            + usage.get("output_tokens", 0) * out
            + usage.get("cache_write", 0) * cw
            + usage.get("cache_read", 0) * cr) / 1e6


def check_compiler(spec: PlaylistSpec, expect: dict) -> list[str]:
    """Return list of mismatched field names."""
    misses = []
    h = spec.hard.model_dump(mode="json")
    for k, want in expect.items():
        got = h.get(k)
        if k in ("exclude_artists", "include_artists", "exclude_tracks"):
            got_l = {x.lower() for x in (got or [])}
            want_l = {x.lower() for x in want}
            if not want_l.issubset(got_l):
                misses.append(k)
        elif str(got) != str(want):
            misses.append(k)
    return misses


def score_playlist(spec: PlaylistSpec, track_ids: list[str],
                   external_meta: dict | None = None) -> dict:
    """Re-run the validator independently; report per-constraint pass/fail."""
    violations = validate(spec, track_ids, external_meta)
    failed = {v.constraint for v in violations}
    h = spec.hard
    checked = {"source_liked_only": h.source == SourceMode.LIKED_ONLY,
               "no_duplicates": h.no_duplicates,
               "track_count": h.track_count is not None,
               "target_duration": h.target_duration_min is not None,
               "year_range": h.year_min is not None or h.year_max is not None,
               "exclude_artists": bool(h.exclude_artists),
               "max_per_artist": h.max_per_artist is not None,
               "include_artists": bool(h.include_artists)}
    per = {c: ("FAIL" if c in failed else "pass") for c, on in checked.items() if on}
    return {"constraints_checked": len(per),
            "constraints_failed": len([c for c in per.values() if c == "FAIL"]),
            "per_constraint": per,
            "violations": [v.to_dict() for v in violations]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--ids", type=str, default="")
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--judge", action="store_true",
                    help="pairwise soft-intent judge agent vs baseline (implies --baseline)")
    args = ap.parse_args()
    if args.judge:
        args.baseline = True

    prompts = {p["id"]: p for p in GOLDEN["prompts"]}
    if args.ids:
        todo = [prompts[i.strip()] for i in args.ids.split(",")]
    elif args.full:
        todo = list(prompts.values())
    else:
        todo = [prompts[i] for i in GOLDEN["smoke"]]

    needs_spotify = any(p["expect"].get("source", "liked_only") != "liked_only"
                        for p in todo)
    sp = None
    if needs_spotify:
        from src.spotify_client import SpotifyClient
        sp = SpotifyClient()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = OUT_BASE / f"{stamp}_{MODEL.replace('claude-', '')}"
    out_dir.mkdir(parents=True)
    print(f"model={MODEL}  prompts={len(todo)}  baseline={args.baseline}")
    print(f"out: {out_dir}\n")

    summary = {"model": MODEL, "stamp": stamp, "results": []}
    total_cost = 0.0

    for p in todo:
        print(f"--- {p['id']} [{p['category']}]: {p['prompt'][:70]}...")
        rec = {"id": p["id"], "category": p["category"], "prompt": p["prompt"]}

        spec = compile_spec(p["prompt"])
        rec["spec"] = spec.model_dump(mode="json")
        rec["compiler_misses"] = check_compiler(spec, p["expect"])
        if rec["compiler_misses"]:
            print(f"    compiler MISS: {rec['compiler_misses']}")

        run = run_agent(spec, spotify_client=sp, verbose=False)
        rec["agent"] = run.to_dict()
        if run.outcome == "infeasible":
            # declaring infeasible is correct iff the golden set says so
            ok = bool(p.get("expect_infeasible"))
            rec["agent_score"] = {
                "constraints_checked": 1, "constraints_failed": 0 if ok else 1,
                "per_constraint": {"feasibility_call": "pass" if ok else "FAIL"},
                "violations": [], "infeasible_reason": run.infeasible_reason}
        elif p.get("expect_infeasible"):
            # produced a playlist where it should have declared infeasibility
            rec["agent_score"] = score_playlist(spec, run.final_track_ids or [])
            rec["agent_score"]["per_constraint"]["feasibility_call"] = "FAIL"
            rec["agent_score"]["constraints_checked"] += 1
            rec["agent_score"]["constraints_failed"] += 1
        else:
            rec["agent_score"] = score_playlist(spec, run.final_track_ids or [])
        c = cost_of(run.usage, MODEL)
        total_cost += c
        first_attempt_clean = (run.violations_history
                               and not run.violations_history[0])
        print(f"    agent: {run.outcome}, {len(run.tool_calls)} calls, "
              f"{run.finalize_attempts} finalizes "
              f"(first {'clean' if first_attempt_clean else 'dirty'}), "
              f"failed {rec['agent_score']['constraints_failed']}"
              f"/{rec['agent_score']['constraints_checked']} constraints, ${c:.3f}")

        if args.baseline:
            from evals.baseline import run_baseline
            b = run_baseline(p["prompt"], spec)
            rec["baseline"] = {k: b[k] for k in ("track_ids", "unresolved", "usage")}
            # unresolved tracks = picks not found in library; append fake ids
            # so the validator counts them as source violations
            fake = [f"NOT_IN_LIBRARY_{i}" for i in range(len(b["unresolved"]))]
            rec["baseline_score"] = score_playlist(spec, b["track_ids"] + fake)
            bc = cost_of(b["usage"], MODEL)
            total_cost += bc
            print(f"    baseline: failed {rec['baseline_score']['constraints_failed']}"
                  f"/{rec['baseline_score']['constraints_checked']} constraints "
                  f"({len(b['unresolved'])}/{len(b['raw_lines'])} picks not in library), ${bc:.3f}")

        if args.judge and run.final_track_ids and rec.get("baseline"):
            from evals.judge import judge_pair
            from src.library import get_tracks
            meta = get_tracks(run.final_track_ids)
            agent_lines = [f"{m['name']} - {', '.join(m['artist_list'])} ({m['year']})"
                           for m in (meta.get(t) for t in run.final_track_ids) if m]
            base_lines = rec_baseline_lines = b["raw_lines"]
            if agent_lines and base_lines:
                verdict = judge_pair(p["prompt"], agent_lines, base_lines,
                                     seed=hash(p["id"]))
                rec["judge"] = verdict
                total_cost += cost_of(verdict["usage"], "claude-haiku-4-5")
                print(f"    judge: winner={verdict['winner']} "
                      f"(vibe={verdict['vibe_fit']}, coherence={verdict['coherence']})")

        (out_dir / f"{p['id']}.json").write_text(
            json.dumps(rec, indent=1), encoding="utf-8")
        summary["results"].append({
            "id": p["id"], "category": p["category"],
            "compiler_misses": rec["compiler_misses"],
            "agent_outcome": rec["agent"]["outcome"],
            "agent_failed": rec["agent_score"]["constraints_failed"],
            "agent_checked": rec["agent_score"]["constraints_checked"],
            "first_attempt_clean": bool(first_attempt_clean),
            "baseline_failed": rec.get("baseline_score", {}).get("constraints_failed"),
            "baseline_checked": rec.get("baseline_score", {}).get("constraints_checked"),
            "judge_winner": rec.get("judge", {}).get("winner"),
        })

    # aggregate
    n = len(summary["results"])
    agg = {
        "prompts": n,
        "compiler_field_accuracy": 1 - sum(bool(r["compiler_misses"]) for r in summary["results"]) / n,
        "agent_clean_rate": sum(r["agent_outcome"] == "clean" for r in summary["results"]) / n,
        "agent_constraint_pass_rate":
            1 - (sum(r["agent_failed"] for r in summary["results"])
                 / max(1, sum(r["agent_checked"] for r in summary["results"]))),
        "first_attempt_clean_rate": sum(r["first_attempt_clean"] for r in summary["results"]) / n,
        "total_cost_usd": round(total_cost, 3),
    }
    if args.baseline:
        agg["baseline_constraint_pass_rate"] = \
            1 - (sum(r["baseline_failed"] or 0 for r in summary["results"])
                 / max(1, sum(r["baseline_checked"] or 0 for r in summary["results"])))
    if args.judge:
        wins = [r["judge_winner"] for r in summary["results"] if r["judge_winner"]]
        if wins:
            agg["judge_agent_wins"] = wins.count("A")
            agg["judge_baseline_wins"] = wins.count("B")
            agg["judge_ties"] = wins.count("tie")
    summary["aggregate"] = agg
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")

    print("\n=== AGGREGATE ===")
    for k, v in agg.items():
        print(f"{k:32s} {v if isinstance(v, int) else round(v, 3)}")


if __name__ == "__main__":
    main()
