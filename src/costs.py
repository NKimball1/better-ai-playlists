"""Shared cost accounting: one price table, one cost function, one ledger.

Every LLM-spending path (CLI playlists, eval runs) prices usage here and
appends to data/cost_ledger.jsonl - so "what has this project cost" is a
one-file answer, not an estimate.
"""
import json
import time
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "data" / "cost_ledger.jsonl"

# $/M tokens: (input, output, cache_write, cache_read)
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


def log_cost(kind: str, detail: str, model: str, usage: dict) -> float:
    """Price usage, append a ledger line, return the dollar cost."""
    cost = cost_of(usage, model)
    LEDGER.parent.mkdir(exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,            # playlist | eval | compiler
            "detail": detail[:120],
            "model": model,
            "usage": usage,
            "cost_usd": round(cost, 4),
        }) + "\n")
    return cost


def ledger_total() -> tuple[float, int]:
    if not LEDGER.exists():
        return 0.0, 0
    rows = [json.loads(l) for l in LEDGER.read_text(encoding="utf-8").splitlines() if l]
    return sum(r["cost_usd"] for r in rows), len(rows)
