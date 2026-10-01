# Demo video — what happens when

**Recording:** submitted separately in the accompanying Drive folder · 3:53 · no audio · browser UI (`uv run generate.py ui`), story `demo`
**Premise:** *A night-shift lift operator realises the elevator keeps stopping at a floor that was demolished 20 years ago.*

The recording and generated demo artifacts are supplied separately in Drive; they are not stored in this Git repository. The Drive submission includes the recording and the complete `runs/demo/` folder so reviewers can inspect the plan, episodes, human-feedback history, saved state, and trace.

Timestamps are approximate (the clips were sped up to fit; waiting on the model is compressed).


| #   | ≈ Time      | What you see                                                                                                                                                                                                                                                                                      | Requirement shown                                                                  |
| --- | ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1   | 0:00 – 1:01 | Entering the premise → the planner creates the 200-episode arc: acts and turning points, characters and arcs, threads with open/resolve targets. The human reviews it and asks for a revision (*"change the setting to a village with a new building, not a city"*); the revised plan appears.    | Plan the full arc · human can **edit** the plan before writing                     |
| 2   | 1:01 – 1:19 | Approving the arc and the threads → generating the 200 episode beats → approving the beats → writing of **episode 1** starts.                                                                                                                                                                     | Human **approves** the plan · beats for all 200 episodes                           |
| 3   | 1:19 – 1:32 | Going through **episode 1**: critic scores (words, revisions, hook, momentum, prose) and issues, the episode text → approving it and starting **episode 2**.                                                                                                                                      | Sequential writing · critic before the human · approve                             |
| 4   | 1:32 – 1:49 | **Rejecting episode 2** with a note (*"end on a scary cliffhanger"*) → the episode is regenerated from that note.                                                                                                                                                                                 | Human can **reject** an episode with feedback                                      |
| 5   | 1:49 – 1:56 | The **regenerated episode 2**, now ending on the requested cliffhanger.                                                                                                                                                                                                                           | Rejection feedback applied                                                         |
| 6   | 1:56 – 2:04 | Stopping the app and coming back: the story reopens at the same point with nothing lost.                                                                                                                                                                                                          | **Resume**                                                                         |
| 7   | 2:04 – 2:40 | Resumed → **feedback for the larger plot** (*"bring in the expert doctor in episode 4, kill her before any conclusion is reached and bring her back as a ghost"*) → its effects: a standing rule, Dr. Selene Patel's arc, thread T7 and act 1 updated, beats rewritten, later beats marked stale. | Feedback that **carries forward** (rule + plan + beats), "kill off this character" |
| 8   | 2:40 – 2:56 | **Manual update** of an episode: the human edits the text directly before approving it; the system builds memory from the human's version.                                                                                                                                                        | Human can **edit** an episode                                                      |
| 9   | 2:56 – 3:10 | The **Memory** tab: Dr. Selene Patel marked **dead** — the part-7 feedback applied in the story itself, not just the plan. Cast, threads and facts as the writer sees them.                                                                                                                       | Consistency across episodes · feedback visible in later episodes                   |
| 10  | 3:10 – 3:48 | **Feedback 2**: *"bring the ghost of the doctor in the next 2 episodes, but make it look like the ghost needs time to manifest fully, and the protagonists are jump-scared at first"* → plan changes, rewritten beats and stale beats shown.                                                      | Second intervention that changes later episodes                                    |
| 11  | 3:48 – 3:53 | **Traceability**: the Trace & cost tab — calls, tokens, cost and latency per step and model, decisions, retries and human actions, the log, and the 200-episode estimate.                                                                                                                         | Traceable · cost-aware · bounded                                                   |




## Where it shows up in the demo artifacts (`runs/demo/` in Drive)

- **Part 1–2:** `arc_plan.md` (plan + 200 beats, revised/stale tags)
- **Part 4–5, 7, 8, 10:** `hitl_log.md` (interventions, the rule each created, and the beats it changed)
- **Part 9:** Selene recorded dead from ep 4; returns only as a ghost (eps 6, 7, 9, 12, 13–15) — `story.md`
- **Part 10:** the planner applied feedback 2 to beats 15–16 rather than eps 7–8, so the gradual manifestation in eps 7–8 (three jump scares) was added as **manual edits** afterwards, recorded in `hitl_log.md`, `state.json`, and `trace.jsonl`
- **Part 11:** `trace.jsonl`

The Drive copy of `runs/demo/` includes `episodes/` (episodes 1–15), `state.json`, and the other files listed above. 

