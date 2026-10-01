# Future scope

What breaks or gets weaker as the story grows, what the 15-episode demo run (`runs/demo`) actually showed,
and how I'd fix each — ordered by impact. Effort: **S** < half a day · **M** 1–2 days · **L** 3+ days.

---

## 1. Quality loop

### 1.1 The critic never passed a draft — **S**
**Seen:** all 15 episodes ended `stopped: max_revisions`; every one was approved by the human over open issues.
9 of 15 are over 700 words (701–882). Critic + rewrites were ~70 % of all episode tokens (≈ 342K of 484K).
**Why:** free models overshoot length and the critic bundles hard rules (length) with taste (hook 7+, prose).
**Fix:**
- Deterministic **length pass** before the critic: if > 700 words, one cheap "cut to 600–650, keep plot and hook" call; never send a length issue to the full revise loop.
- Split critic output into **blocking** (contradictions, dead characters, broken rules, length) and **advisory** (prose, momentum); only blocking issues trigger a rewrite.
- Calibrate thresholds on a few human-approved episodes (the human approved hook 6 / prose 5 drafts — the bar is above what the human needs).

### 1.2 Prose quality on free models — **S (config) / M (eval)**
**Seen:** critic prose scores 4–7; some generic phrasing slips through the banned-phrase list.
**Fix:** the architecture is model-agnostic — set `WRITER_MODEL` to a Claude/GPT-class model. Add 2–3 human-approved
exemplar passages to the cached part of the writer prompt, and learn a banned-phrase list from human edits (diff of edited vs drafted text).

### 1.3 Voice drift when switching models — **S**
**Seen:** the writer changed 120B → 20B → Qwen → 120B across eps 1–15 due to quotas.
**Fix:** a short "voice card" (sentence length, POV tics, recurring imagery) extracted from approved episodes and added to the brief; pin one writer model per act.

---

## 2. Memory and consistency at 150+ episodes

### 2.1 Duplicate identities — **S**
**Seen:** the doctor is recorded dead as both "Dr. Selene Patel" (ep 4) and "Selene Patel" (ep 5) — two cast entries.
**Fix:** give the extractor the canonical cast list and require it to map names to it; add an alias table (titles, first names, nicknames) and fuzzy matching before a new character is created.

### 2.2 Keyword fact retrieval — **M**
**Problem:** facts are chosen by word overlap with the beat; paraphrases ("the fire" vs "the 2004 blaze") are missed, and facts accumulate (~1,600 by ep 200).
**Fix:** embeddings over facts; facts keyed by entity (character / place / object) so every on-page entity pulls its own facts; periodic merge of duplicate and superseded facts ("door locked" → "door forced open in ep 40").

### 2.3 Critic only sees retrieved facts — **M**
**Problem:** a contradiction with an old fact that wasn't retrieved for the *beat* slips through.
**Fix:** second retrieval pass on the entities that actually appear in the **draft**, before the critic runs; a cheap nightly full-story audit (summaries + facts) that flags contradictions across the whole run.

### 2.4 Chapter summaries don't scale forever — **M**
**Problem:** 10-episode summaries are fine to ~ep 150 (~14 summaries, ~2K tokens) but lose detail and grow linearly.
**Fix:** hierarchical memory — act summaries over chapter summaries, with drill-down retrieval into a chapter when the beat references it.

### 2.5 Arc drift vs. the plan — **M**
**Problem:** character arcs in the plan only change through explicit feedback; if the story drifts, the writer still sees the original arc.
**Fix:** an end-of-act review: compare planned arcs/threads with what the records say happened, propose plan updates, human approves.

---

## 3. Human-in-the-loop

### 3.1 Stale beats are marked, not re-planned — **S**
**Seen:** after "kill the doctor", 41 later beats still mention her (40 after the ghost note); the writer adapts at write time using the stale warning.
**Fix:** a "re-plan stale beats" action (per chunk of 10, on demand or when within N episodes), with a before/after diff for the human. The re-planning code existed and was deliberately removed to keep costs and control with the human.

### 3.2 Standing rules pile up — **S**
**Problem:** every feedback can add a rule; rules can overlap or contradict ("may return as a ghost" vs "stays dead").
**Fix:** the feedback step merges/supersedes existing rules and shows the human the resulting rule set; rules can be scoped to an episode range.

### 3.3 Retcon flags but doesn't repair — **M**
**Problem:** editing ep 40 replaces its record and flags conflicting later episodes, but fixing them is manual (`regen`).
**Fix:** ordered repair: regenerate or patch flagged episodes oldest-first, re-extract each record, re-check downstream; versioned records so a retcon can be undone.

### 3.4 Scripted vs. live interventions — **S**
The demo's interventions were typed by the human; `demo.py` can also script them for reproducible runs. A "review queue" UI (batch of drafts, approve/reject in bulk) would make long runs practical.

---

## 4. Cost, speed, limits

### 4.1 Token use per episode (~28K) — **S/M**
**Fix (in order of payoff):** fix the critic loop (1.1, ~–50 %); prompt caching of the stable brief prefix; skip the LLM critic when code checks pass on a clean streak; cheap-model draft + strong-model revise only on failure; batch API for extraction/summaries.

### 4.2 Free-tier quotas — **S**
**Seen:** 6 rate-limit waits, 8 prompt-too-big fallbacks, writer switched 3 times. 200K tokens/day/model ≈ 20 episodes/day.
**Fix:** paid/Developer tier (≈ 10× limits) — at measured usage 200 episodes cost ≈ $2 at list prices; automatic model rotation when a quota is hit instead of stopping.

### 4.3 Throughput — **M**
Writing is inherently sequential (each episode needs the previous record), but extraction, chapter summaries, beat planning and audits can run in parallel/background while the human reviews.

---

## 5. Product and engineering

| Item | Effort |
|---|---|
| Hosted web app (auth, per-user stories, spending cap) — the UI is already a thin layer over the engine | M |
| Branching: fork a story at episode N to try two directions | M |
| Evaluation harness: consistency/repetition metrics over a fixed set of premises, regression-tested per prompt change | M |
| Unit tests for memory fold, retcon, feedback propagation (currently exercised via the mock provider) | S |
| Export to EPUB / per-episode publishing with recap lines | S |
| Multi-language serials (brief + critic in the target language) | M |
