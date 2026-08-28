# Better AI Playlists

A natural-language playlist agent for Spotify that **actually honors hard constraints** —
built because Spotify's own AI playlist feature can't handle requests like
*"only use songs I've already liked."*

```
"60 min workout playlist, only songs I've liked, nothing from Drake, max 2 per artist"
   │
   ▼
┌─────────────────┐   typed spec    ┌──────────────────────────────┐
│ Constraint       │ ──────────────▶ │ Agent loop (bounded, gated)  │
│ compiler (1 LLM  │  hard ▸ code    │  search_library / search_    │
│ call, structured │  soft ▸ intent  │  spotify* / finalize         │
│ output)          │                 │  *only offered when the spec │
└─────────────────┘                 │   allows non-library tracks  │
                                    └──────────────┬───────────────┘
                                                   │ finalize(track_ids)
                                                   ▼
                                    ┌──────────────────────────────┐
                                    │ Deterministic validator      │
                                    │  every hard constraint,      │
                                    │  zero LLM. Violations return │
                                    │  as repair instructions ────▶│──┐
                                    └──────────────┬───────────────┘  │ repair
                                             clean │                  │ loop
                                                   ▼                  │ (≤4)
                                            playlist on Spotify   ◀───┘
```

## Why this architecture

**The core thesis: separate constraints from taste.** A prompt-to-playlist
feature fails on "only songs I've liked" because it treats the whole prompt as
vibes. Here, one structured-output call compiles the prompt into a typed
`PlaylistSpec` with two halves:

- **Hard constraints** (source mode, counts, durations, year ranges, artist
  caps/exclusions) — enforced by *code*, never by the model.
- **Soft intent** (vibe, genres, energy arc) — the agent's quality bar,
  scored by an LLM judge, never "enforced."

Anything the compiler can't place in either bucket lands in an `ambiguities`
list instead of being silently dropped — the exact failure mode this project
exists to fix.

**Tool gating is policy, not prompt.** In `liked_only` mode the Spotify
search tool isn't in the agent's tool list at all. The model can't be talked
into using a tool it doesn't have.

**The agent can't ship a playlist directly.** `finalize_playlist` runs the
validator; violations come back as structured repair instructions ("Future
appears 3 times as primary artist, max is 2; remove 1"). The loop ends only
on clean validation or exhausted budget.

**Cheap model + hard validator.** The agent runs on Haiku 4.5 (~$0.05/run
with prompt caching, vs ~$0.40 on Opus). The validator makes model choice a
cost knob rather than a correctness risk: a weaker model takes more repair
rounds, not wrong results. Measured, not assumed — see evals.

## The hostile-API subplot

In Nov 2024–Feb 2026 Spotify removed the endpoints this kind of project used
to rely on: recommendations, audio features, related artists — and stripped
`genres` and `popularity` from all dev-mode responses. So retrieval is built
from scratch:

- Library synced to SQLite (4.2K tracks) with FTS5 full-text search
- Genre layer rebuilt via **MusicBrainz** artist tags (1 req/s, resumable
  fetcher, ~1.6K artists)
- Hard filters pushed down into SQL — the agent physically can't retrieve a
  track that violates year/artist constraints

## Evaluation

Two measured layers, one judged layer:

1. **Compiler accuracy** — compiled specs vs. golden expected fields on a
   30-prompt set spanning constraint types, source modes, and adversarial
   cases ("50 liked songs all released in 1995").
2. **Constraint satisfaction** — the validator re-scores every final playlist;
   headline metric is pass rate *by constraint type*, plus repair-loop stats
   (first-attempt-clean rate quantifies what the validator repair cycle earns).
3. **Soft intent** — pairwise LLM judge (agent vs. naive baseline), position-
   randomized, scoped to what code can't check: vibe fit, coherence, ordering.

**Baseline:** one LLM call, no tools, no library access — an honest stand-in
for the prompt-to-playlist feature pattern. Instructive early result: on
liked-only prompts the unmodified baseline either *declines outright* or picks
songs the user never saved (up to 14/20), while the agent passes 100% of
constraint checks (smoke set).

```
python -m evals.run_eval --smoke              # 5 prompts, ~$0.25
python -m evals.run_eval --full --judge       # full sweep, ~$3
python -m pytest tests/ -q                    # validator unit tests (free)
```

## Run it

```
pip install -r requirements.txt
cp .env.example .env    # Spotify client id + Anthropic key; see comments
python spike_library.py            # OAuth + pull your library
python -m src.enrich_musicbrainz   # genre enrichment (resumable, ~45 min)
python -m src.build_db             # build the corpus DB
python playlist.py "20 sad songs from my liked music" [--create]
```

`--create` writes the playlist to your Spotify account (private).

## Constraints of the platform (dev mode, Feb 2026)

- 5 users max, owner needs Premium — this is a personal tool by design
- Search capped at 10 results/call; no batch fetches — which is exactly why
  the agent loop exists: retrieval has to be iterative and budgeted
