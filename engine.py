"""Story engine: arc planning, layered memory, write -> critic -> revise loop, HITL operations.

State model: state.json holds the plan, beats, directives and one *delta* per approved episode.
Story memory (cast, facts, threads, timeline) is never stored directly: it is derived by folding
deltas 1..n-1. So editing a past episode = replacing one delta; everything downstream re-derives.
"""
import json, os, re
from pathlib import Path
from llm import LLM, PromptTooBig

TOTAL = int(os.getenv("TOTAL_EPISODES", "200"))
MIN_W, MAX_W = 400, 700
MAX_REVISIONS = int(os.getenv("MAX_REVISIONS", "2"))
EP_COST_CAP = float(os.getenv("EP_COST_CAP", "0.03"))       # USD per episode (list price; ~3x a normal episode)
# Groq free tier: 200K tokens/day *per model*, so each role gets its own model (3x the daily budget).
WRITER = os.getenv("WRITER_MODEL", "openai/gpt-oss-120b")    # write + revise
CHEAP = os.getenv("CHEAP_MODEL", "openai/gpt-oss-120b")       # critic, extraction, chapter summaries
PLANNER = os.getenv("PLANNER_MODEL", "openai/gpt-oss-120b")     # arc, beats, feedback replans, retcon impact
FALLBACK = os.getenv("FALLBACK_MODEL", "openai/gpt-oss-120b")  # used when a model keeps returning bad JSON
PLAN_KEYS = ["title", "logline", "tone", "setting", "world_rules", "characters", "acts", "threads", "ending"]
BEAT_CHUNK = int(os.getenv("BEAT_CHUNK", "10"))              # episodes per beat-expansion call
CRITIC = os.getenv("CRITIC_MODEL", CHEAP)
RECENT, CHAPTER, STALE = 8, 10, 15
DEAD = {"dead", "deceased", "killed"}

WRITER_SYS = """You are the head writer of a long-running serialized story. Rules:
- 400-700 words (target ~550). Output 'TITLE: <title>' on the first line, then prose only.
- Execute THIS episode's beat. Never resolve future beats early.
- Something must change every episode: a reveal, a decision, a reversal, or a loss.
- End on a hook: a concrete new question, threat, or revelation in the final 1-3 lines. Not a vague mood.
- Continuity is sacred: obey the world rules, character status, known facts and timeline in the brief.
- Prose: specific nouns, sensory detail, dialogue with subtext, varied rhythm. Never open with a recap.
  Banned: 'little did', 'a testament to', 'tapestry', 'couldn't help but', 'a chill ran down', 'the weight of',
  'in that moment', ending on a moral or a summary.
- SHOWRUNNER DIRECTIVES override everything else."""

CRITIC_SYS = ("You are a ruthless continuity editor and story editor for a serial. You catch contradictions, "
              "repeated plot moves, weak hooks, generic prose, and ignored showrunner directives. Be specific.")


def _j(o):
    return json.dumps(o, ensure_ascii=False)


def _slim(ctx):
    """Trimmed writer brief for calls that also carry a full draft (critic, revise): drops the long
    'plot moves used' list and chapter summaries, keeps only the last 4 recent-episode summaries."""
    out = []
    for sec in ctx.split("\n\n# "):
        head = sec.split("\n", 1)[0]
        if head.startswith(("PLOT MOVES ALREADY USED", "STORY SO FAR")):
            continue
        if head.startswith("RECENT EPISODES"):
            lines = sec.split("\n")
            sec = "\n".join([lines[0]] + lines[1:][-4:])
        out.append(sec)
    return "\n\n# ".join(out)


def _text(x):
    """Critic issues should be strings; some models return {"issue": ..., "detail": ...} objects instead."""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return " | ".join(str(v) for v in x.values() if v) or json.dumps(x, ensure_ascii=False)
    return str(x)


def _words(t):
    return {w for w in re.findall(r"[a-z]+", t.lower()) if len(w) > 3}


def _grams(t, k=5):
    w = re.findall(r"[a-z']+", t.lower())
    return {tuple(w[i:i + k]) for i in range(len(w) - k + 1)}


class Story:
    def __init__(self, name):
        self.dir = Path(os.getenv("RUNS_DIR", "runs")) / name
        (self.dir / "episodes").mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "state.json"
        self.s = json.loads(self.path.read_text()) if self.path.exists() else None
        self.llm = LLM(self.dir)

    def save(self):  # atomic write: a crash mid-save never corrupts the run
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.s, indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    # ---------- episode files ----------
    def ep_path(self, n):
        return self.dir / "episodes" / f"ep_{n:03d}.md"

    def write_ep(self, n, title, text):
        self.ep_path(n).write_text(f"# Episode {n}: {title}\n\n{text.strip()}\n")

    def read_ep(self, n):
        raw = self.ep_path(n).read_text() if self.ep_path(n).exists() else ""
        head, _, body = raw.partition("\n")
        if head.startswith("# "):
            return head.split(":", 1)[-1].strip(), body.strip()
        return "", raw.strip()

    def approved(self, below=None):
        return sorted(int(k) for k, e in self.s["episodes"].items()
                      if e.get("status") == "approved" and (below is None or int(k) < below))

    def hitl(self, **kw):
        self.s["hitl"].append(kw)
        self.llm.log(step="hitl", **kw)

    # ---------- 1. planning ----------
    PLAN_SCHEMA = """Return JSON only:
{"title": str, "logline": str, "tone": str, "pov": "e.g. close third on X, past tense", "setting": str,
 "world_rules": [hard rules of how this world / mystery works; never to be contradicted],
 "characters": [{"name": str, "role": "protagonist|antagonist|ally|...", "desc": str,
                 "arc": "who they are at ep 1 -> who they are at the end", "secret": str}],
 "acts": [{"n": int, "title": str, "start": int, "end": int, "summary": str, "turning_point": str}],
 "threads": [{"id": "T1", "desc": str, "opens_by": int, "resolves_by": int}],
 "ending": str}"""

    def new(self, premise):
        user = (f"Premise: {premise}\n\nDesign a {TOTAL}-episode serial (400-700 words per episode, each ending on "
                f"a hook). It needs an escalating mystery, a midpoint reversal, arcs that change people, and an ending "
                f"that pays off the premise. 7-8 acts covering episodes 1-{TOTAL} contiguously, 6-10 characters, "
                f"8-14 threads (clues, relationships, secrets) with open/resolve targets.\n{self.PLAN_SCHEMA}")
        plan = self.llm.call("plan_arc", "You are a veteran showrunner who plans long serials with rigorous continuity.",
                             user, PLANNER, 5000, json_out=True,
                             need=PLAN_KEYS, fallback=FALLBACK)
        self.s = {"premise": premise, "status": "plan_review", "plan": plan, "beats": {}, "beat_history": [],
                  "directives": [], "episodes": {}, "chapters": {}, "hitl": [], "next_ep": 1}
        self.save()

    def revise_plan(self, note):
        user = f"Current plan:\n{_j(self.s['plan'])}\n\nShowrunner notes: {note}\n\nRevise the plan.\n{self.PLAN_SCHEMA}"
        self.s["plan"] = self.llm.call("plan_revise", "You are a veteran showrunner.", user, PLANNER, 5000,
                                       json_out=True, need=PLAN_KEYS, fallback=FALLBACK)
        self.s["beats"] = {}
        self.hitl(ep=0, type="plan_feedback", text=note)
        self.save()

    def expand_beats(self, progress=print):
        """One beat per episode, BEAT_CHUNK episodes per call (small requests fit free-tier limits).
        Resumable: finished chunks are skipped."""
        p = self.s["plan"]
        for a in p["acts"]:
            for s in range(a["start"], a["end"] + 1, BEAT_CHUNK):
                e = min(s + BEAT_CHUNK - 1, a["end"])
                if all(str(k) in self.s["beats"] for k in range(s, e + 1)):
                    continue
                prev = [{"ep": k, "beat": self.s["beats"][str(k)]["beat"]} for k in range(s - 5, s) if str(k) in self.s["beats"]]
                user = (f"SERIES PLAN (compact):\n{_j(self.plan_brief(a['n']))}\n\nCURRENT ACT {a['n']} \"{a['title']}\" "
                        f"(eps {a['start']}-{a['end']}): {a['summary']}\nAct ends with: {a['turning_point']}\n\n"
                        f"PREVIOUS BEATS: {_j(prev)}\n\nWrite one beat for each of episodes {s}-{e}.\n"
                        "Rules: every beat makes a DIFFERENT plot move; vary locations and pressure; plant clues before "
                        f"payoffs; {'episode ' + str(a['end']) + ' delivers the act turning point; ' if e == a['end'] else ''}"
                        "honor thread opens_by/resolves_by.\nReturn JSON "
                        '{"beats":[{"ep":int,"beat":"1-2 sentences: what happens and what changes","hook":"ending hook",'
                        '"chars":[names on-page],"threads":[thread ids touched]}]}')
                r = self.llm.call("expand_beats", "You are a showrunner breaking a season into episodes.", user,
                                  PLANNER, 1800, json_out=True, need=["beats"], fallback=FALLBACK)
                for b in r.get("beats", []):
                    if s <= int(b["ep"]) <= e:
                        self.s["beats"][str(int(b["ep"]))] = b
                self.save()
                progress(f"  beats for eps {s}-{e} (act {a['n']}) done")
        for e in range(1, TOTAL + 1):
            self.s["beats"].setdefault(str(e), {"ep": e, "beat": "(unplanned: advance the act toward its turning point)",
                                                "hook": "", "chars": [], "threads": []})
        self.save()

    def plan_brief(self, act_n=None):
        """Plan without prose-heavy fields: keeps planner prompts small."""
        p = self.s["plan"]
        return {"title": p["title"], "logline": p["logline"], "tone": p["tone"], "setting": p["setting"],
                "world_rules": p["world_rules"], "ending": p["ending"],
                "characters": [{"name": c["name"], "role": c["role"], "arc": c["arc"]} for c in p["characters"]],
                "acts": [{"n": a["n"], "title": a["title"], "eps": f"{a['start']}-{a['end']}",
                          "turning_point": a["turning_point"]} for a in p["acts"] if a["n"] != act_n],
                "threads": [{"id": t["id"], "desc": t["desc"], "opens_by": t.get("opens_by"),
                             "resolves_by": t.get("resolves_by")} for t in p["threads"] if not t.get("dropped")]}

    # ---------- 2. memory (derived by folding per-episode deltas) ----------
    def memory(self, upto):
        p = self.s["plan"]
        chars = {c["name"]: {"status": "alive", "desc": c.get("desc", ""), "arc": c.get("arc", ""),
                             "role": c.get("role", ""), "notes": [], "last_seen": 0} for c in p["characters"]}
        threads = {t["id"]: {"desc": t["desc"], "status": "planned", "opened": None, "last": None,
                             "opens_by": t.get("opens_by"), "resolves_by": t.get("resolves_by")} for t in p["threads"]}
        m = {"chars": chars, "threads": threads, "rels": {}, "facts": [], "sigs": [], "timeline": ""}
        for n in self.approved(below=upto):
            d = self.s["episodes"][str(n)].get("delta", {})
            for c in d.get("characters", []):
                ch = chars.setdefault(c["name"], {"status": "alive", "desc": c.get("change", ""), "arc": "",
                                                  "role": "introduced ep %d" % n, "notes": [], "last_seen": n})
                ch["status"] = (c.get("status") or ch["status"]).lower()
                ch["last_seen"] = n
                if c.get("change"):
                    ch["notes"].append(f"ep{n}: {c['change']}")
            for r in d.get("relationships", []):
                m["rels"]["|".join(sorted([r["a"], r["b"]]))] = f"{r['state']} (ep{n})"
            m["facts"] += [{"ep": n, "f": f} for f in d.get("facts", [])]
            for t in d.get("threads_opened", []):
                th = threads.setdefault(t["id"], {"desc": t.get("desc", ""), "opens_by": None, "resolves_by": None})
                th.update(status="open", opened=n, last=n)
            for tid in d.get("threads_advanced", []):
                if tid in threads:
                    threads[tid].update(last=n, status="open" if threads[tid]["status"] != "closed" else "closed")
            for tid in d.get("threads_closed", []):
                if tid in threads:
                    threads[tid].update(status="closed", last=n)
            m["sigs"].append((n, d.get("signature", "")))
            m["timeline"] = d.get("timeline") or m["timeline"]
        for t in p["threads"]:  # threads dropped by human feedback no longer nag the writer
            if t.get("dropped"):
                threads.pop(t["id"], None)
        return m

    def history(self, n):
        """Chapter summaries for old blocks, individual summaries for the recent window (and any gaps)."""
        recent_start, chs, singles = max(1, n - RECENT), [], []
        ok = set(self.approved(below=n))
        for s in range(1, n, CHAPTER):
            e, key = s + CHAPTER - 1, f"{s}-{s + CHAPTER - 1}"
            if e < recent_start and key in self.s["chapters"]:
                chs.append((key, self.s["chapters"][key]))
            else:
                singles += [k for k in range(s, min(e, n - 1) + 1) if k in ok]
        return chs, singles

    def make_chapter(self, start):
        key, eps = f"{start}-{start + CHAPTER - 1}", range(start, start + CHAPTER)
        if not all(str(k) in self.s["episodes"] and self.s["episodes"][str(k)].get("status") == "approved" for k in eps):
            return
        lines = "\n".join(f"Ep {k}: {self.s['episodes'][str(k)]['delta'].get('summary', '')}" for k in eps)
        self.s["chapters"][key] = self.llm.call(
            "chapter", "You compress serial episodes into continuity summaries.",
            f"{lines}\n\nSummarize episodes {key} in <=120 words. Keep: who learned what, deaths/injuries, "
            f"relationship shifts, objects/clues introduced, unresolved questions. No flourish.", CHEAP, 400).strip()
        self.save()

    def ensure_chapters(self, n):
        for s in range(1, max(1, n - RECENT), CHAPTER):
            if s + CHAPTER - 1 < n - RECENT and f"{s}-{s + CHAPTER - 1}" not in self.s["chapters"]:
                self.make_chapter(s)

    def act_of(self, n):
        acts = self.s["plan"]["acts"]
        return next((a for a in acts if a["start"] <= n <= a["end"]), acts[-1])

    def directives_text(self, n):
        ds = [d for d in self.s["directives"] if d["active"] and d["from_ep"] <= n]
        return "\n".join(f"- [{d['id']}, since ep {d['from_ep']}] {d['text']}" for d in ds)

    def context(self, n):
        """The writer's brief for episode n. Bounded size regardless of n (~3-5k tokens)."""
        p, m, B = self.s["plan"], self.memory(n), self.s["beats"]
        beat = B.get(str(n), {})
        nxt = [B[str(k)] for k in (n + 1, n + 2) if str(k) in B]
        act, out = self.act_of(n), []
        out.append(f"# SERIES BIBLE\nTitle: {p['title']}\nLogline: {p['logline']}\nTone: {p['tone']}\n"
                   f"POV: {p.get('pov', '')}\nSetting: {p['setting']}\nWorld rules (never contradict):\n- "
                   + "\n- ".join(p["world_rules"]))
        if d := self.directives_text(n):
            out.append("# SHOWRUNNER DIRECTIVES (mandatory; override plan and beats)\n" + d)
        out.append(f"# POSITION\nEpisode {n} of {TOTAL}. Act {act['n']} \"{act['title']}\" (eps {act['start']}-"
                   f"{act['end']}): {act['summary']}\nAct ends with: {act['turning_point']}\n"
                   f"Series ending (foreshadow only): {p['ending']}")
        chs, singles = self.history(n)
        if chs:
            out.append("# STORY SO FAR (chapter summaries)\n" + "\n".join(f"[Eps {k}] {v}" for k, v in chs))
        if singles:
            eps = self.s["episodes"]
            out.append("# RECENT EPISODES\n" + "\n".join(
                f"Ep {k} '{eps[str(k)].get('title', '')}': {eps[str(k)]['delta'].get('summary', '')} "
                f"| ended on: {eps[str(k)]['delta'].get('hook', '')}" for k in singles))
        if n > 1 and str(n - 1) in self.s["episodes"]:
            tail = " ".join(self.read_ep(n - 1)[1].split()[-220:])
            out.append(f"# END OF PREVIOUS EPISODE (verbatim; pick up from here)\n...{tail}")
        if m["timeline"]:
            out.append(f"# TIMELINE\nIn-story time at end of last episode: {m['timeline']}")
        # cast: full detail for who's on-page or recent, one line for everyone else (nobody is forgotten)
        focus = set(beat.get("chars", [])) | {c for b in nxt for c in b.get("chars", [])}
        focus |= {c for c, v in m["chars"].items() if "protagonist" in v["role"].lower() or v["last_seen"] >= n - 4 > 0}
        full, rest = [], []
        for c, v in m["chars"].items():
            if c in focus:
                full.append(f"- {c} [{v['status'].upper()}] {v['role']}: {v['desc']} | arc: {v['arc']} | recent: "
                            + ("; ".join(v["notes"][-3:]) or "not yet on-page"))
            else:
                gone = v["last_seen"] and n - v["last_seen"] > 25 and v["status"] not in DEAD
                rest.append(f"{c} ({v['status']}, last seen ep {v['last_seen'] or '-'}{', OFF-PAGE TOO LONG' if gone else ''})")
        rels = [f"- {k.replace('|', ' & ')}: {v}" for k, v in m["rels"].items() if any(x in focus for x in k.split("|"))]
        out.append("# CAST\n" + "\n".join(full) + ("\nOthers: " + "; ".join(rest) if rest else "")
                   + ("\nRelationships:\n" + "\n".join(rels) if rels else ""))
        th = []
        for tid, t in m["threads"].items():
            if t["status"] == "open":
                flag = f" [STALE since ep {t['last']}: advance soon]" if n - (t["last"] or n) > STALE else ""
                flag += " [OVERDUE: resolve]" if t.get("resolves_by") and t["resolves_by"] < n else ""
                th.append(f"- {tid} OPEN (since ep {t['opened']}): {t['desc']}{flag}")
            elif t["status"] == "planned" and (t.get("opens_by") or 999) <= n + 2:
                th.append(f"- {tid} PLANNED, should open by ep {t['opens_by']}: {t['desc']}")
        if th:
            out.append("# OPEN THREADS\n" + "\n".join(th))
        q = _words(_j(beat) + _j(nxt))
        facts = sorted(m["facts"], key=lambda f: (len(q & _words(f["f"])), f["ep"]), reverse=True)[:12]
        facts += [f for f in m["facts"] if f["ep"] >= n - 3 and f not in facts]
        if facts:
            out.append("# ESTABLISHED FACTS (retrieved; do not contradict)\n"
                       + "\n".join(f"- (ep{f['ep']}) {f['f']}" for f in sorted(facts, key=lambda f: f["ep"])))
        if m["sigs"]:
            out.append("# PLOT MOVES ALREADY USED (do not repeat)\n" + "; ".join(f"ep{k}: {s}" for k, s in m["sigs"][-30:]))
        out.append(f"# THIS EPISODE'S BEAT\n{beat.get('beat', '')}\nPlanned hook: {beat.get('hook', '')}\n"
                   f"On-page: {', '.join(beat.get('chars', []))} | Threads: {', '.join(beat.get('threads', []))}\n"
                   f"Coming next (do NOT execute yet): " + " / ".join(b.get("beat", "") for b in nxt))
        if beat.get("stale"):  # planned before a human change and not re-planned: the writer must adapt it
            out.append(f"# WARNING: THIS BEAT IS STALE\nIt was planned before this showrunner change: {beat['stale']}\n"
                       "Keep the beat's dramatic purpose, but follow the SHOWRUNNER DIRECTIVES, CAST status and "
                       "current plan wherever they conflict with it (e.g. a character who is now dead cannot appear).")
        return "\n\n".join(out)

    # ---------- 3. write -> critic -> revise ----------
    def _split(self, raw):
        m = re.match(r"\s*TITLE:\s*(.+)\n", raw)
        return (m.group(1).strip(), raw[m.end():].strip()) if m else ("Untitled", raw.strip())

    def overlap(self, n, text):
        g, worst = _grams(text), 0.0
        for k in range(max(1, n - 6), n):
            if self.ep_path(k).exists() and g:
                worst = max(worst, len(g & _grams(self.read_ep(k)[1])) / len(g))
        return worst

    def critique(self, n, title, text, ctx):
        wc, hard = len(text.split()), []
        if not MIN_W <= wc <= MAX_W:
            hard.append(f"Word count is {wc}; must be {MIN_W}-{MAX_W}.")
        rep = self.overlap(n, text)
        if rep > 0.06:
            hard.append(f"Reuses phrasing from recent episodes ({rep:.0%} 5-gram overlap). Rephrase.")
        dead = [c for c, v in self.memory(n)["chars"].items()
                if v["status"] in DEAD and c.split()[0].lower() in text.lower()]
        check = f"\nCODE CHECK: characters marked DEAD appear in the text: {dead}. Flag unless it's memory/evidence or allowed by world rules." if dead else ""
        user = (f"{ctx}\n\n# DRAFT OF EPISODE {n}: {title}\n{text}\n{check}\n\n# REVIEW THE DRAFT AGAINST THE BRIEF\n"
                'Return JSON: {"hook": 1-10, "momentum": 1-10, "prose": 1-10, "beat_followed": bool, '
                '"consistency_issues": [contradictions with facts/cast status/timeline/world rules; quote both sides], '
                '"repetition_issues": [plot moves, reveals or images that repeat earlier episodes], '
                '"directive_violations": [showrunner directives ignored], "fix_notes": "concrete revision instructions"}\n'
                "Hook 7+ means a reader must click the next episode. Empty lists if none.")
        c = self.llm.call("critic", CRITIC_SYS, user, CRITIC, 1500, json_out=True, ep=n,
                          need=["hook", "momentum"], fallback=FALLBACK)
        for k in ("consistency_issues", "repetition_issues", "directive_violations"):  # models sometimes return objects
            c[k] = [_text(i) for i in (c.get(k) or []) if i] if isinstance(c.get(k), list) else ([_text(c[k])] if c.get(k) else [])
        issues = hard + c["consistency_issues"] + c["repetition_issues"] + c["directive_violations"]
        if not c.get("beat_followed", True):
            issues.append("Did not execute this episode's beat.")
        if c.get("hook", 10) < 7:
            issues.append(f"Hook too weak ({c.get('hook')}/10).")
        if c.get("momentum", 10) < 6:
            issues.append(f"Momentum too low ({c.get('momentum')}/10).")
        c.update(word_count=wc, phrase_overlap=round(rep, 3), issues=issues, passed=not issues)
        return c

    def _fit(self, fn, ctx):
        """Call fn with the full brief; if it can't fit the provider's per-minute token limit,
        retry once with the trimmed brief (_slim). Raises PromptTooBig if even that doesn't fit."""
        try:
            return fn(ctx)
        except PromptTooBig:
            return fn(_slim(ctx))

    def produce(self, n, note=None):
        """Draft, then up to MAX_REVISIONS critic-driven rewrites, bounded by EP_COST_CAP."""
        self.ensure_chapters(n)
        ctx = self.context(n)
        if note:
            ctx += f"\n\n# SHOWRUNNER NOTE FOR THIS EPISODE (previous draft was rejected)\n{note}"
        start = self.llm.spent(n)
        task = f"\n\n# TASK\nWrite episode {n}."
        title, text = self._split(self._fit(lambda c: self.llm.call("write", WRITER_SYS, c + task, WRITER, 2000, ep=n), ctx))
        attempt, crit = 0, None
        for attempt in range(MAX_REVISIONS + 1):
            try:
                crit = self._fit(lambda c: self.critique(n, title, text, c), ctx)
            except PromptTooBig as e:  # can't review within the limit: hand the draft to the human as-is
                crit = {"issues": [f"Not reviewed: {e}"], "passed": False, "stopped": "prompt_too_big",
                        "word_count": len(text.split())}
                break
            cost = self.llm.spent(n) - start
            self.llm.log(step="decision", ep=n, attempt=attempt, passed=crit["passed"], issues=crit["issues"], ep_cost=round(cost, 4))
            if crit["passed"]:
                break
            stop = "max_revisions" if attempt == MAX_REVISIONS else "cost_cap" if cost >= EP_COST_CAP else None
            if stop:
                self.llm.log(step="decision", ep=n, event="stop_revising", reason=stop)
                crit["stopped"] = stop
                break
            fix = "\n".join(f"- {i}" for i in crit["issues"]) + f"\n{crit.get('fix_notes', '')}"
            try:
                raw = self._fit(lambda c: self.llm.call("revise", WRITER_SYS, (
                    f"{c}\n\n# YOUR PREVIOUS DRAFT\nTITLE: {title}\n{text}\n\n# EDITOR NOTES (fix all)\n{fix}\n\n"
                    f"Rewrite the full episode {n}. Keep what works."), WRITER, 2000, ep=n), ctx)
            except PromptTooBig:       # keep the current draft rather than crash
                self.llm.log(step="decision", ep=n, event="stop_revising", reason="prompt_too_big")
                crit["stopped"] = "prompt_too_big"
                break
            title, text = self._split(raw)
        self.write_ep(n, title, text)
        e = self.s["episodes"].setdefault(str(n), {})
        e.update(status="draft", title=title, critic=crit, revisions=attempt, words=len(text.split()),
                 cost=round(self.llm.spent(n) - start, 4))
        self.save()

    # ---------- 4. memory extraction on approval ----------
    def extract(self, n, title, text):
        m = self.memory(n)
        known = ", ".join(f"{c} [{v['status']}]" for c, v in m["chars"].items())
        threads = {k: f"{v['desc']} [{v['status']}]" for k, v in m["threads"].items() if v["status"] != "closed"}
        user = (f"Known characters: {known}\nThreads: {_j(threads)}\n"
                f"Last in-story time: {m['timeline'] or 'start'}\n\nEPISODE {n}: {title}\n{text}\n\n"
                "Extract the continuity record as JSON:\n"
                '{"summary": "60-90 words, plain", "hook": "how it ends", "signature": "the plot move, <12 words", '
                '"timeline": "in-story day/time at the end", "facts": [max 8 durable checkable facts: names, numbers, '
                'objects, injuries, who knows what], "characters": [{"name", "status": "alive|dead|missing|unknown", '
                '"change": "what changed for them"}], "relationships": [{"a", "b", "state"}], '
                '"threads_opened": [{"id", "desc"}], "threads_advanced": [ids], "threads_closed": [ids]}\n'
                f"Use existing thread ids (a PLANNED thread appearing on-page goes in threads_opened with its id). "
                f"New thread ids: E{n}-1, E{n}-2. Only characters on-page or whose status changed.")
        d = self.llm.call("extract", "You are a meticulous script supervisor.", user, CHEAP, 1500, json_out=True, ep=n,
                          need=["summary", "facts"], fallback=FALLBACK)
        # normalise shapes (models occasionally return objects where strings are expected)
        for k in ("summary", "hook", "signature", "timeline"):
            d[k] = _text(d.get(k) or "")
        d["facts"] = [_text(f) for f in (d.get("facts") or []) if f]
        for k in ("threads_advanced", "threads_closed"):
            d[k] = [x if isinstance(x, str) else str(x.get("id", x)) if isinstance(x, dict) else str(x) for x in (d.get(k) or [])]
        d["threads_opened"] = [t for t in (d.get("threads_opened") or []) if isinstance(t, dict) and t.get("id")]
        d["characters"] = [dict(c, change=_text(c.get("change") or "")) for c in (d.get("characters") or [])
                           if isinstance(c, dict) and c.get("name")]
        d["relationships"] = [r for r in (d.get("relationships") or []) if isinstance(r, dict) and r.get("a") and r.get("b")]
        return d

    def approve(self, n, edited=False):
        title, text = self.read_ep(n)
        e = self.s["episodes"][str(n)]
        e.update(delta=self.extract(n, title, text), status="approved", title=title, words=len(text.split()))
        if edited:
            e["edited"] = True
            self.hitl(ep=n, type="edit", text="human edited the episode text before approval")
        self.s["next_ep"] = max(self.s["next_ep"], n + 1)
        self.save()
        if n % CHAPTER == 0:
            self.make_chapter(n - CHAPTER + 1)

    # ---------- 5. HITL: feedback that propagates ----------
    def feedback(self, text, n):
        """A human note propagates at three levels:
        (a) standing directive the writer AND critic see from ep n on,
        (b) the plan itself: character arcs, threads, act summaries/turning points,
        (c) beats: the next FEEDBACK_HORIZON are rewritten now; later beats touching the affected
            characters/dropped threads are only MARKED stale (no re-planning). The writer is told the
            beat predates the feedback, and a human can edit it later."""
        horizon = int(os.getenv("FEEDBACK_HORIZON", "25"))
        up = {k: {x: v.get(x) for x in ("beat", "hook", "chars", "threads")}
              for k, v in self.s["beats"].items() if n <= int(k) < n + horizon}
        user = (f"PLAN (compact): {_j(self.plan_brief())}\nCurrent directives:\n"
                f"{self.directives_text(n) or '(none)'}\n\nNext episode to write: {n}.\nSHOWRUNNER FEEDBACK: \"{text}\"\n\n"
                f"Upcoming beats: {_j(up)}\n\nReturn JSON: {{\"directive\": \"a precise standing instruction applied to "
                "every future episode (e.g. pacing limits, banned moves, who is dead); empty if purely a one-time event\", "
                "\"revised_beats\": [{\"ep\": int, \"beat\": str, \"hook\": str, \"chars\": [..], \"threads\": [..]}] "
                "only upcoming beats that must change, "
                "\"plan_updates\": {\"characters\": [{\"name\": str, \"arc\": \"new arc incl. when/how it ends\"}], "
                "\"threads\": [{\"id\": str, \"desc\": str, \"opens_by\": int, \"resolves_by\": int, \"drop\": bool}], "
                "\"acts\": [{\"n\": int, \"summary\": str, \"turning_point\": str}]} "
                "only entries the feedback changes (e.g. a killed character's arc, threads that depended on them), "
                "\"rationale\": str}")
        r = self.llm.call("feedback", "You are a showrunner adjusting a running serial.", user, PLANNER, 3500, json_out=True,
                          need=["directive"], fallback=FALLBACK)
        tag = f"feedback@ep{n}"
        changed = []
        for b in r.get("revised_beats", []):
            k = str(int(b["ep"]))
            if int(k) >= n and k in self.s["beats"]:
                self.s["beat_history"].append({"ep": int(k), "old": self.s["beats"][k], "feedback": text})
                self.s["beats"][k] = {**b, "revised_by": tag}
                changed.append(int(k))
        plan_changes = self._apply_plan_updates(r.get("plan_updates") or {}, tag)
        # later beats that mention an affected character/dropped thread are marked stale (not re-planned)
        names = set(plan_changes["characters"])
        # only DROPPED threads invalidate beats; a reworded/re-timed thread is picked up via the plan brief
        tids = {t["id"] for t in self.s["plan"]["threads"] if t.get("dropped") and t["id"] in plan_changes["threads"]}
        stale = []
        for k, b in self.s["beats"].items():
            if int(k) >= n + horizon and int(k) not in changed and (
                    names & set(b.get("chars", [])) or tids & set(b.get("threads", [])) or
                    any(x.split()[0].lower() in b.get("beat", "").lower() for x in names)):
                b["stale"] = f"{tag}: {text}"
                stale.append(int(k))
        if r.get("directive"):
            self.s["directives"].append({"id": f"D{len(self.s['directives']) + 1}", "text": r["directive"],
                                         "source": text, "from_ep": n, "active": True})
        self.hitl(ep=n, type="feedback", text=text, directive=r.get("directive"), beats_changed=changed,
                  plan_changes=plan_changes, beats_stale=len(stale), rationale=r.get("rationale"))
        self.save()
        return r, changed

    def _apply_plan_updates(self, u, tag):
        p, done = self.s["plan"], {"characters": [], "threads": [], "acts": []}
        for c in u.get("characters", []):
            ch = next((x for x in p["characters"] if x["name"] == c.get("name")), None)
            if ch and c.get("arc"):
                ch.setdefault("arc_history", []).append(ch["arc"])
                ch.update(arc=c["arc"], revised_by=tag)
                done["characters"].append(ch["name"])
        for t in u.get("threads", []):
            th = next((x for x in p["threads"] if x["id"] == t.get("id")), None)
            if th is None and t.get("id") and t.get("desc") and not t.get("drop"):
                th = {"id": t["id"], "desc": t["desc"]}
                p["threads"].append(th)
            if th:
                th.update({k: t[k] for k in ("desc", "opens_by", "resolves_by") if t.get(k)}, revised_by=tag)
                if t.get("drop"):
                    th["dropped"] = True
                done["threads"].append(th["id"])
        for a in u.get("acts", []):
            act = next((x for x in p["acts"] if x["n"] == a.get("n")), None)
            if act:
                act.update({k: a[k] for k in ("summary", "turning_point") if a.get(k)}, revised_by=tag)
                done["acts"].append(act["n"])
        return done

    def retcon(self, n):
        """A past, approved episode was edited: replace its delta, invalidate summaries, flag downstream conflicts."""
        e = self.s["episodes"][str(n)]
        old = e.get("delta", {})
        title, text = self.read_ep(n)
        new = self.extract(n, title, text)
        e.update(delta=new, title=title, edited=True)
        for k in list(self.s["chapters"]):
            a, b = map(int, k.split("-"))
            if a <= n <= b:
                del self.s["chapters"][k]
        later = [k for k in self.approved() if k > n]
        r = {"conflicts": [], "continuity_note": ""}
        if later:
            eps = self.s["episodes"]
            lines = "\n".join(f"Ep {k}: {eps[str(k)]['delta'].get('summary', '')} FACTS: {eps[str(k)]['delta'].get('facts', [])}" for k in later)
            r = self.llm.call("retcon_impact", CRITIC_SYS, (
                f"Episode {n} was rewritten by the showrunner.\nOLD record: {_j(old)}\nNEW record: {_j(new)}\n\n"
                f"Later episodes:\n{lines}\n\nReturn JSON {{\"conflicts\": [{{\"ep\": int, \"issue\": str}}], "
                "\"continuity_note\": \"instruction for future episodes to reconcile the change\"}"), PLANNER, 2000, json_out=True,
                need=["conflicts"], fallback=FALLBACK)
            for c in r.get("conflicts", []):
                if str(c["ep"]) in eps:
                    eps[str(c["ep"])]["flag"] = c["issue"]
            if r.get("continuity_note"):
                self.s["directives"].append({"id": f"D{len(self.s['directives']) + 1}", "from_ep": self.s["next_ep"],
                                             "text": f"CONTINUITY (retcon of ep {n}): {r['continuity_note']}",
                                             "source": "retcon", "active": True})
        self.hitl(ep=n, type="retcon", conflicts=r.get("conflicts", []), note=r.get("continuity_note"))
        self.save()
        return r

    # ---------- reporting ----------
    def estimate(self):
        rows = [json.loads(l) for l in self.llm.trace.read_text().splitlines()] if self.llm.trace.exists() else []
        cost, lat = {}, {}
        for r in rows:
            if "cost" in r:
                cost[r.get("ep")] = cost.get(r.get("ep"), 0) + r["cost"]
                lat[r.get("ep")] = lat.get(r.get("ep"), 0) + r.get("latency", 0)
        eps = [k for k in self.approved() if k in cost]
        plan_cost = cost.get(None, 0)
        if not eps:
            return "No approved episodes yet: rough guess is ~$0.05-0.08 and ~45s per episode on Sonnet-class + Haiku-class models."
        c, t = sum(cost[k] for k in eps) / len(eps), sum(lat[k] for k in eps) / len(eps)
        return (f"Measured over {len(eps)} episodes: ${c:.4f} and {t:.0f}s of model time per episode.\n"
                f"Planning (arc + {TOTAL} beats + feedback/chapters): ${plan_cost:.2f}\n"
                f"Projected {TOTAL} episodes: ${plan_cost + c * TOTAL:.2f}, ~{t * TOTAL / 3600:.1f} h sequential "
                f"(plus human review time).")
