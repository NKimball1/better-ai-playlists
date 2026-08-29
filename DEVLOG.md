# Devlog

Chronological build record: what was decided, why, and what the numbers said.
(Companion to the README, which describes the finished architecture.)

## 2026-08-28 — Day 1: the whole vertical slice

### 1. Problem framing
Spotify's AI playlist feature ignores hard requirements like "only songs I've
liked." Thesis: that failure comes from treating the whole prompt as vibes.
Fix: **separate constraints from taste** — compile hard constraints into a
typed spec enforced by code; let the LLM own only retrieval and aesthetics.

### 2. Platform recon (changed the whole design)
Verified against a live dev-mode app: Spotify killed recommendations, audio
features, and related artists for new apps (Nov 2024), and Feb 2026 dev mode
additionally strips `genres` and `popularity` from every response (confirmed:
0/20 sampled artists had genres, 0/4194 tracks had popularity). Search caps at
10 results; batch fetches gone. **Consequence: retrieval and ranking must be
built from scratch — which is the interesting part.** ISRCs survive, so
external joins are possible.

### 3. Corpus
- Pulled 4,194 saved tracks (1,671 artists) via PKCE OAuth into SQLite + FTS5.
- Rebuilt the genre layer with MusicBrainz artist tags: 1 req/s, resumable,
  ordered by track count so head artists enrich first (33 artists in, 45% of
  tracks already had a tag). Final: **1,559/1,671 matched (93%), 3,380/4,194
  tracks (81%) with ≥1 tag**.
- Bugs hit: Windows cp1252 console crash on `Ē` (fix: utf-8 stdout);
  `A$AP`-style names break Lucene (fix: sanitized retry — recovered all of
  A$AP Mob except Ferg, a standing miss). Known limitation: same-name
  collisions (MEDUZA the house trio matched a rockabilly band).

### 4. Architecture decisions
- **Constraint compiler**: one structured-output call → `PlaylistSpec` with
  `hard` (machine-checkable) / `soft` (judged) / `ambiguities` (what couldn't
  be captured — never silently dropped).
- **Tool gating is policy, not prompt**: `liked_only` mode does not include
  the Spotify search tool in the API request at all.
- **Manual agent loop** over SDK tool-runner: we own budgets (30 tool calls,
  4 finalize attempts), traces, and the repair cycle.
- **Validator**: pure code, returns violations as actionable repair
  instructions. Semantic choices unit-tested (17 tests): exclusions count
  features; max_per_artist counts primary only; missing year ≠ fail-closed.
- **finalize is the only exit**: the agent cannot ship an unvalidated list.

### 5. Cost engineering (after a budget scare)
First Opus run: ~$0.40. Levers applied: prompt caching (every loop turn
re-reads the prefix at 10%), agent on Haiku 4.5 — safe *because* the
validator catches its mistakes (measured: Haiku needed 3 finalize attempts
where Opus needed 1, same clean result). **Run cost: ~$0.05 (8.5x cut).**
Full 30-prompt sweep with baseline + judge: **$1.43**.

### 6. Eval design
- 30 golden prompts across constraint types, 3 source modes, adversarial cases.
- Layer 1: compiler field accuracy vs hand-written expectations.
- Layer 2: constraint satisfaction — validator re-scores agent AND baseline
  against the same spec.
- Layer 3: pairwise judge, position-randomized, scoped to what code can't
  check (vibe/coherence only).
- Baseline: one LLM call, no tools/library — the naive prompt-to-playlist
  pattern. First version *declined* liked-only requests ("I can't see your
  library"); hardened it to always produce a list so the comparison is fair.

### 7. Full sweep results (run `20260828_170937`, Haiku agent)
| metric | agent | baseline |
|---|---|---|
| constraint pass rate | **91.9%** | 57.6% |
| source_liked_only | **25/25** | **0/25** |
| year_range | 5/5 | 1/5 |
| target_duration | 5/10 | 0/10 |
| judge (soft intent) | 12 wins | 10 wins |

- Compiler: 100% field accuracy (30/30).
- First-attempt-clean: 53% → the repair loop produced nearly half of all
  correct results.
- Judge near-tie → aesthetics at parity; constraints are the differentiator.
  (Judge itself not yet calibrated against owner labels — pending.)

### 8. Failure analysis → fixes
8 failures, two clusters:
1. **Duration arithmetic** (5/10): Haiku sums durations badly under combo
   constraints, burns finalize attempts. Fix: `playlist_stats` tool — a free
   dry-run calculator (totals, artist caps, dupes) so the model never does
   math in its head.
2. **Infeasible specs**: "50 liked songs from 1995" (library has 1) ground to
   budget death. Fix: `report_infeasible(reason, evidence)` tool + eval
   scoring that rewards a correct infeasibility call and punishes a lazy one.

### 9. Shipped
Public repo: https://github.com/NKimball1/better-ai-playlists
(library snapshot, tokens, and run traces stay local — gitignored).

### 10. The h28 saga (4-hour marathon prompt) — a case study in agent budgeting
Post-fix rerun: 7/8 previously-failed prompts now pass (96% on the subset);
`h27` correctly declares infeasibility with evidence. But `h28` ("4 hour
liked-songs marathon, max 3 per artist") kept failing, each attempt exposing
a different design flaw:

1. **Flat tool budget** (30 calls) doesn't fit a ~65-track assembly.
   Fix: budget scales with requested playlist size (240 min → 69 calls).
2. **Flat repair budget** (4 finalize attempts): each round on a 65-track
   list juggles more state; Haiku fixed one violation while creating another.
   Fix: finalize attempts also scale with size.
3. **Escape-hatch misuse**: given more attempts, the agent *declared the
   feasible task infeasible* after 5 failed repairs — gave a model an out,
   and under pressure it took it. The eval caught it (`feasibility_call:
   FAIL` — lazy infeasibility claims are scored as failures). Fix: tool
   description hardened — infeasibility requires counted evidence from
   searches; "assembly is difficult" doesn't qualify. Next run it fought
   honestly to budget exhaustion instead.
4. **Spec-semantics bug**: ±5 min tolerance on a 240-min request (2%) is not
   what "4 hour marathon" means. Fix: tolerance defaults proportional
   (max(5 min, 4%)); the compiler sets it explicitly only when the user is
   precise ("exactly an hour").

After all four fixes Haiku still exhausts its budget (43 calls, 6 finalizes,
3/4 constraints held, duration keeps missing). Standing conclusion: 60+ track
joint-constraint assembly exceeds Haiku's working capacity in this loop.

### 11. Model-tier experiment settles it
Same prompt, same architecture, Opus 5: **clean on the first finalize** —
13 tool calls, 69 tracks, 239.2/240 min, 0 violations, $0.63, 224s. Haiku
had burned ~$1.50 across four failed attempts on the same task.

Conclusion: the ceiling was the model, not the architecture — and the spec
tells us the assembly size *before spending anything*. Shipped as routing:
estimated assembly ≤40 tracks → Haiku (~$0.05); larger → Opus. Every prompt
in the golden set now has a passing configuration; h28 passes via routing.

### 12. Judge calibration against owner labels
10 blinded pairs (agent vs baseline, order randomized), labeled by the
library owner on pure vibe fit:

- **Owner blind preference: agent 7, tie 3, baseline 0.** The baseline's
  famous-song picks lost every time ("I don't like Lizzo").
- **Judge agreement with owner: 4/10** — near coin-flip. In 5 of 6
  disagreements the judge preferred the baseline: it recognizes canonical
  tracks and scores them as "coherent," a systematic popularity bias.

Consequence: the judge's earlier 12–10 verdict is downgraded to
context-only; **constraint satisfaction (measured, 99%) is the headline
metric**. Caveat recorded: blinding was imperfect — the owner sometimes
recognized their own library, so owner preference partly measures
familiarity (for a personal-playlist product, arguably the point).

## Open items
- Re-run the 8 failed prompts post-fix; then full-sweep regression.
- Judge calibration: ~10 owner hand-labels vs judge verdicts.
- HTML eval report.
- Maybe: embeddings for semantic retrieval (tags+FTS have been sufficient so far).

### 13. Cost telemetry audit
Hand-auditing the ~$6 total spend against recorded eval costs surfaced a gap:
`total_cost_usd` counted agent + baseline + judge tokens but not the Opus
constraint-compiler call that starts every run — ~20% of true spend,
invisible in the telemetry. Fixed: compiler usage is now recorded per prompt
and priced in, and agent runs are priced by the model routing actually chose
(a routed Opus run was previously priced as Haiku). Lesson: cost telemetry
is a measurement system too, and it needs auditing like any other.
