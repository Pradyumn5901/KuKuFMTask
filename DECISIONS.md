# DECISIONS

**Shape:** plain Python with a planner → writer → critic → extractor loop. There's no agent framework: the control flow is a fixed loop, so a graph library would add nothing. All state lives in one JSON file, and the story's memory is **derived** from it, never stored directly.

## 1. How does the system remember the story at episode 150?

Every approved episode produces a small structured **delta**: a summary, a hook, a one-line "plot move" signature, the in-story time, up to 8 checkable facts, character status changes, relationship changes, and threads opened, advanced or closed. Story memory is the **fold of deltas 1..n-1**. The writer's brief has the same bounded size at episode 5 and at episode 150 (about 5–6k tokens):

| Layer | What goes in | How it's chosen |
|---|---|---|
| Plan | bible, world rules, current act and its turning point, the ending | always included (small, static, cacheable) |
| Directives | standing human instructions | always included; they override the plan |
| Long history | one ~120-word summary per 10 episodes | summarised when a block finishes; ~14 at ep 150 |
| Recent | last 8 episode summaries, plus the last 220 words of the previous episode verbatim | sliding window, for voice and cliffhanger continuity |
| Cast | full cards for on-page, recent and protagonist characters; **one line for everyone else** with status and last-seen episode | nobody drops out of context. "Off-page too long" is flagged at 25 episodes |
| Threads | every open thread; STALE after 15 untouched episodes; OVERDUE past its resolve-by; PLANNED threads due to open | derived from deltas and plan targets |
| Facts | top 12 by keyword overlap with this beat and the next two, plus facts from the last 3 episodes | simple lexical retrieval. Good enough for a POC; embeddings are the upgrade |
| Anti-repeat | last 30 plot-move signatures | "do not repeat" list |

Because memory is a fold, **editing history is cheap**. Retconning episode 40 replaces one delta, invalidates the 31–40 chapter summary (rebuilt lazily), and runs an impact check over episodes 41+. That check flags conflicting episodes (`regen` rewrites them) and adds a continuity directive for everything still unwritten.

## 2. Where does the human step in, and why there?

- **Arc and beats, before any writing.** This is the cheapest place to change the story: every later token depends on it.
- **Every episode, before its delta enters memory.** Approval is the commit point. Anything unapproved never shapes later episodes, so a bad draft can't poison the memory. Edits are re-extracted, so the human's text is what gets remembered.
- **Feedback at any point**, turned into two things: (a) a **standing directive** that the writer must follow and the critic must enforce in every later episode, and (b) **rewrites of up to the next 25 beats**, so "kill off X" changes the plan itself and isn't just a hint. Before/after beats are logged as proof.
- **Not** inside the critic/revise loop. That loop is bounded and automatic. Humans should judge story, not word counts.

## 3. How is inconsistency or repetition caught before a human sees it?

1. **Code checks (free):** word count; 5-gram overlap with the last 6 episodes; dead characters named in the text.
2. **LLM critic** (cheap model, same brief as the writer): consistency against the retrieved facts, cast status, timeline and world rules; repeated plot moves compared with the signature list; directive violations; whether the beat was followed; hook and momentum scores.
3. **Structural flags in the brief:** stale or overdue threads, overdue planned threads, and characters off-page too long. These push the writer before a problem happens.

A draft that fails gets up to 2 revisions, capped at $0.30 per episode. After that it goes to the human with its open issues shown.

## 4. What breaks first as the story grows, and how would I fix it?

1. **Fact retrieval.** Lexical overlap misses paraphrases ("the fire" vs "the 1998 blaze"), and the fact list grows by about 1,600 entries over 200 episodes. *Fix:* embeddings over facts, plus entity-keyed facts (per character, place and object), plus periodic merging of duplicate or superseded facts.
2. **The critic only sees what was retrieved.** A contradiction with an unretrieved fact from ep 20 slips through. *Fix:* a second pass that pulls facts by the entities *in the draft* (not just the beat), and a nightly full-story audit.
3. **Chapter summaries lose detail**, and 20 of them stop being cheap. *Fix:* summaries of summaries (act level), with drill-down retrieval into chapters.
4. **Directives pile up and conflict.** *Fix:* the feedback step merges or retires older directives (manual `directive-off` exists today).
5. **Retcons ripple.** Today: flag, regen, and a continuity note. Long-term: version deltas per episode and re-run extraction on regenerated episodes in order.
6. **Voice drift and "LLM prose".** The critic's prose score is weak. *Fix:* a few human-approved exemplar passages in the cached prefix, and a banned-phrase list learned from edits.
