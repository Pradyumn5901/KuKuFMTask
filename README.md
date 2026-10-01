# Serial — a 200-episode story writer with a human in the loop

Give it a one-line premise. It plans a 200-episode arc (acts, turning points, character arcs, threads),
then writes episodes one at a time — each one drafted, critiqued, revised, and **approved, edited or rejected by a human**.
Human feedback ("kill off this character", "slow down the romance") becomes a standing rule *and* rewrites the plan,
so it changes future episodes, not just the current one. Stop at any point; it resumes exactly where it left off.

Plain Python, no agent framework. CLI + a vanilla-HTML browser UI over the same engine and the same saved state.

---

## Quick start (≈ 5 minutes)

Requires [uv](https://docs.astral.sh/uv/) and a free Groq key (https://console.groq.com/keys).

```bash
uv sync                                   # installs groq + truststore into .venv
cp .env.example .env                      # put GROQ_API_KEY=gsk_... in .env

uv run generate.py ui                     # browser UI → http://localhost:8765
# or the CLI:
uv run generate.py --premise "A delivery rider realizes every address on today's route belongs to someone who died in the same building."
uv run generate.py create 5               # write + review the next 5 episodes
```

Try everything without a key or tokens: `LLM_PROVIDER=mock uv run generate.py ui`.

---

## Demo run (`runs/demo/`)

| | |
|---|---|
| **Premise** | A night-shift lift operator realises the elevator keeps stopping at a floor that was demolished 20 years ago |
| **Title** | *The Forgotten Floor* — 6 acts, 6 characters, 10 threads, 200 beats |
| **Written** | 15 episodes approved (ep 1–15), chapter summary for 1–10 |
| **Models** | `gpt-oss-120b`, `gpt-oss-20b`, `qwen3.8-27b` on Groq's free tier — the writer was switched mid-run when daily quotas ran out (state carried over unchanged) |

**Human interventions, and what they changed**

| When | Intervention | Effect |
|---|---|---|
| Plan | *"Change the setting to a village with a new building, not a city"* | Planner revised the whole arc (village, new community building, sealed 7th floor) before any writing |
| Plan | Approved arc + beats | Writing unlocked |
| Ep 2 | **Reject**: *"end on a scary cliffhanger"* | Episode rewritten from the note |
| Ep 4 | **Feedback**: *"bring in the expert doctor in episode 4, kill her before any conclusion is reached, bring her back as a ghost"* | Standing rule D1 (*killed characters may only return as a supernatural presence*); Dr. Selene Patel's arc + thread T7 + act 1 rewritten; 2 beats rewritten; 41 later beats marked stale |
| Ep 4 | **Edit** by hand before approval | Memory extracted from the human's version |
| Ep 6 | **Feedback**: *"bring the ghost of the doctor in the next 2 episodes, but make it look like the ghost needs time to manifest"* | Arc + T7 + act 3 updated; 2 beats rewritten; 40 later beats marked stale |

**Evidence the feedback carried forward:** Selene is recorded **dead from ep 4**; from then on she appears only as a
ghost (eps 6, 7, 9, 12, 15 — e.g. ep 15: *"the translucent eyes of Selene"*), never as a living character.

Outputs: `runs/demo/arc_plan.md` (plan + 200 beats, revised/stale tags), `story.md` (all approved episodes),
`hitl_log.md` (every intervention), `episodes/ep_NNN.md`, `state.json`, `trace.jsonl`; plus `logs/serial.log`.
Regenerate the .md files with `uv run generate.py export --run demo`.

---

## How it works

```
premise ─► PLANNER: arc (acts, turning points, character arcs, threads)  ─► HUMAN approves / edits / revises
        ─► PLANNER: 200 beats, 10 per call                                ─► HUMAN approves / edits
        ─► for each episode:
             WRITER draft ─► CRITIC (code checks + LLM) ─► WRITER revise (≤ 2) ─► HUMAN approve / edit / reject / feedback
             ─► EXTRACTOR writes the episode's memory record ─► SUMMARIZER every 10 episodes
```

Five LLM roles in one fixed loop — the order never changes, so agents/graphs would add cost and opacity, not capability.

| Role | Does | Default model (`.env` var) |
|---|---|---|
| Planner | arc, beats, feedback → plan changes, retcon impact | `qwen/qwen3.8-27b` (`PLANNER_MODEL`) |
| Writer | draft + revisions | `openai/gpt-oss-120b` (`WRITER_MODEL`) |
| Critic | consistency, repetition, rule violations, hook/momentum/prose scores | `openai/gpt-oss-20b` (`CRITIC_MODEL`) |
| Extractor | approved text → memory record | `openai/gpt-oss-20b` (`CHEAP_MODEL`) |
| Summarizer | 10-episode chapter summaries | `openai/gpt-oss-20b` (`CHEAP_MODEL`) |

One model per role because Groq's free tier gives **each model its own 200K tokens/day**. If a model keeps returning
unusable JSON it switches to `FALLBACK_MODEL` for the rest of the session.

### Memory — how episode 150 stays consistent

Each approved episode produces a small **record**: summary, hook, one-line plot move, in-story time, ≤ 8 facts,
character changes (incl. alive/dead), relationships, threads opened/advanced/closed.
Memory is **never stored directly** — it is rebuilt by replaying records 1…n-1, so editing history is cheap and only
human-approved text ever enters memory.

The writer's brief has the same structure (≈ 5–6K tokens) at episode 5 and episode 150:

1. bible + world rules · 2. standing rules from human feedback · 3. current act + turning point
4. **chapter summaries** (one per 10 episodes, older than the last 8) · 5. **last 8 episode summaries**
6. last ~220 words of the previous episode verbatim · 7. in-story time
8. **cast** — full cards for who's on-page/recent, one line (status, last seen) for everyone else; *off-page too long* flag
9. **threads** — open, stale (> 15 eps untouched), overdue, planned-due · 10. **~12 retrieved facts** + recent ones
11. last 30 plot moves (*don't repeat*) · 12. this beat (+ next two, foreshadow only) · stale-beat warning if any

### Catching problems before a human does

1. **Code checks**: 400–700 words; > 6 % 5-gram overlap with the last 6 episodes; a dead character named in the text.
2. **LLM critic** with the same brief: contradictions, repeated plot moves, broken standing rules, beat followed, hook ≥ 7, momentum ≥ 6.
3. **Brief flags**: stale/overdue threads, characters off-page too long.

### Human in the loop

* **Arc + beats** — approve, revise with a note, or edit the JSON. Cheapest place to change the story.
* **Every episode, before it enters memory** — approve · approve & write next · reject with a note · edit the text yourself · give feedback.
* **Feedback** propagates on four levels: a **standing rule** (writer must follow, critic enforces) · **plan updates**
  (character arcs, threads changed/dropped, act summaries) · **next 25 beats rewritten** · later beats that mention affected
  characters or dropped threads **marked stale** (the writer is told to follow current rules over them; not auto re-planned).
* **Retcon** — edit an already-approved episode: its record is replaced, memory for later episodes rebuilds, the chapter
  summary is rebuilt, later episodes are checked and flagged, a continuity rule is added.

### Resume, bounds, observability

* **Resume** — `runs/<story>/state.json` is written atomically after every step; `create` / *Write* continues from the next episode, an unreviewed draft is shown again. You can switch models between sessions.
* **Bounds** — ≤ 2 critic-driven revisions (`MAX_REVISIONS`), $0.03 per episode at list price (`EP_COST_CAP`), 6 API retries, stop at ep 200. Requests are sized to Groq's 8K tokens/min; a 413 retries smaller, a 429 waits exactly as long as Groq says, an exhausted daily quota stops cleanly; prompts that still don't fit fall back to a trimmed brief.
* **Trace** — `runs/<story>/trace.jsonl`: every call (step, episode, model, tokens, cost, latency, attempt) + decisions, retries, fallbacks, human actions. `logs/serial.log`: the same, human-readable, `[file.py] -- time --- message --- line N` with tracebacks.

---

## Cost and time (measured on the demo run)

| | Demo (plan + 200 beats + 15 episodes) |
|---|---|
| LLM calls | 130 |
| Tokens | 561K (planning + beats ≈ 77K; **≈ 28K per episode**, median 7 calls) |
| Cost at Groq list prices | **$0.17** (≈ $0.01 per episode) — $0 actually paid on the free tier |
| Model time | median ≈ 3 min per episode (free-tier latency, excludes rate-limit waits and review) |

**Projection for 200 episodes:** ≈ 5.7M tokens, **≈ $2 at list prices**, ≈ 10 h of model time sequentially.
On the free tier the binding limit is 200K tokens/day per model (≈ 600K/day across three models) → about 20 episodes/day.
`uv run generate.py estimate --run <story>` recomputes this from your own trace.

**How to cut it**

* **Fix the critic/length loop** — in the demo the critic never passed a draft (mostly word count). Episodes used 2–3 critic calls and 1–2 rewrites; passing first time would cut per-episode tokens by ~50 %.
* Prompt caching of the stable brief prefix (bible, rules, chapter summaries) — cached tokens are cheaper and don't count toward Groq's limits.
* Skip the LLM critic when code checks pass and the previous episode passed; critique every other episode.
* Draft with a cheap model, revise with a strong one only on failure.
* Batch API for extraction and chapter summaries (not latency-sensitive).

---

## Commands

```bash
uv run generate.py ui                          # browser UI (http://localhost:8765)
uv run generate.py --premise "..."  [--fresh]  # plan arc + threads + 200 beats, review them
uv run generate.py plan                        # re-open arc/beat review
uv run generate.py create 5 [--auto]           # write + review the next 5 episodes
uv run generate.py feedback "Kill off Mara within 3 episodes"
uv run generate.py edit 4                      # retcon an approved episode ($EDITOR)
uv run generate.py regen 13 --note "show the death on-page"
uv run generate.py status | estimate | export
uv run generate.py off D2                      # switch off standing rule D2
# add --run NAME to work on a story other than the default "story"
```

Episode review keys (CLI): `a` approve · `e` edit · `r` reject + note · `f` feedback · `v` critic report · `q` quit.

**UI tabs:** Plan · Episodes · Feedback · Memory (cast/threads/facts + the writer's exact brief) · Retcon · Trace & cost.
Long LLM steps run in the background with a live log; CLI and UI share the same state, so you can switch between them.

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `GROQ_API_KEY` | — | required (unless `LLM_PROVIDER=mock`) |
| `WRITER_MODEL` / `CRITIC_MODEL` / `CHEAP_MODEL` / `PLANNER_MODEL` / `FALLBACK_MODEL` | see table above | any Groq model; `claude-…`, `gemini-…`, `ollama:…`, `openrouter:…` also routed |
| `GROQ_REASONING` / `GROQ_REASONING_CHEAP` / `QWEN_REASONING` | `medium` / `low` / `default` | reasoning effort (fewer tokens when lower) |
| `MAX_REVISIONS` · `EP_COST_CAP` | `2` · `0.03` | per-episode bounds |
| `FEEDBACK_HORIZON` · `BEAT_CHUNK` · `TOTAL_EPISODES` | `25` · `10` · `200` | |
| `GROQ_TPM` · `LLM_RPM` · `LLM_RETRIES` | `8000` · `0` · `6` | rate-limit handling |
| `FREE_TIER` | `0` | `1` logs free-tier calls as $0 instead of list price |
| `LOG_LEVEL` · `LOG_DIR` · `RUNS_DIR` · `UI_PORT` | `INFO` · `logs` · `runs` · `8765` | |
| `SSL_CERT_FILE` | — | corporate proxy CA bundle (the OS trust store is used by default) |

Other providers: `uv sync --extra claude` or `uv sync --extra openai` (Gemini / OpenRouter / Ollama).

## Files

| File | |
|---|---|
| `generate.py` | single entry point (CLI + `ui`) |
| `main.py` | CLI review loop, plan review, export, status, `.env` loading |
| `engine.py` | planning, memory, write → critic → revise loop, extraction, feedback propagation, retcon |
| `llm.py` | provider routing, rate limits, retries, JSON validation + fallback, trace, cost, logging |
| `ui.py` | browser UI (stdlib HTTP server + one vanilla HTML/JS page) |
| `setup.md` | fresh-clone installation and run instructions |
| `Video.md` | demo recording walkthrough and artifact guide |

## Known limitations

Seen in the demo run — details and fixes in [FUTURE_SCOPE.md](FUTURE_SCOPE.md):

* **The critic never passed a draft**: all 15 episodes were approved by the human despite unresolved critic issues; 5 reached the 2-revision limit. Based on the saved run word counts, 10 of 15 episodes exceed 700 words.
* **Duplicate identities**: the doctor was recorded as both "Dr. Selene Patel" and "Selene Patel" (exact-name matching).
* **Keyword fact retrieval**: paraphrased facts can be missed, and the critic only sees retrieved facts.
* **Stale beats are marked, not re-planned**: 41 beats still mention the doctor after her death; the writer adapts at write time.
* **Free-tier models**: flatter prose (critic prose scores 4–7); daily quotas forced writer switches mid-story
  (120B for eps 1–5 → 20B for 5–9 → Qwen for 9–10 → 120B again for 10–15), so the voice varies slightly.
