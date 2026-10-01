"""CLI for the serial writer.  uv run main.py --help
Other providers: uv sync --extra claude  |  uv sync --extra openai  (Gemini/OpenRouter/Ollama)"""
import argparse, json, os, shlex, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)  # runs/, logs/ and .env always live next to the code

# Load .env (KEY=value lines) before engine reads its settings. Real env vars win.
if os.path.exists(".env"):
    for _line in open(".env"):
        _k, _sep, _v = _line.strip().partition("=")
        if _sep and not _k.startswith("#"):
            os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

from engine import Story, TOTAL  # noqa: E402
from llm import get_logger, LOG_FILE  # noqa: E402

log = get_logger("main")
VIA_X = os.path.basename(sys.argv[0]) == "generate.py"   # started through generate.py: show its hints


def hint(kind, name):
    x = "" if name == "story" else f" --run {name}"
    return {"new": f'uv run generate.py --premise "..."{x}' if VIA_X else f'uv run main.py new {name} "premise"',
            "resume": f"uv run generate.py create 5{x}" if VIA_X else f"uv run main.py write {name}",
            "next": f"uv run generate.py create 1{x}" if VIA_X else f"uv run main.py episode {name}"}[kind]


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def open_editor(path):
    subprocess.call(shlex.split(os.getenv("EDITOR", "nano")) + [str(path)])


def edit_json(obj):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    while True:
        open_editor(f.name)
        try:
            return json.load(open(f.name))
        except json.JSONDecodeError as e:
            if ask(f"Invalid JSON ({e}). Re-open editor? [y/n] ") != "y":
                return obj


def load(name):
    st = Story(name)
    if not st.s:
        sys.exit(f"No story named '{name}'. Start one with: {hint('new', name)}")
    return st


# ---------------- plan review (HITL gate #1) ----------------
def print_plan(p):
    print(f"\n=== {p['title']} ===\n{p['logline']}\nTone: {p['tone']} | POV: {p.get('pov')}\nSetting: {p['setting']}")
    print("World rules:", *[f"\n  - {r}" for r in p["world_rules"]])
    print("Characters:", *[f"\n  - {c['name']} ({c['role']}): {c['arc']}" for c in p["characters"]])
    print("Acts:", *[f"\n  {a['n']}. [{a['start']}-{a['end']}] {a['title']}: {a['turning_point']}" for a in p["acts"]])
    print("Threads:", *[f"\n  {t['id']}: {t['desc']} (open by {t['opens_by']}, resolve by {t['resolves_by']})" for t in p["threads"]])
    print(f"Ending: {p['ending']}\n")


def review_plan(st):
    while st.s["status"] == "plan_review":
        print_plan(st.s["plan"])
        c = ask("[a]pprove arc  [e]dit JSON  [f]eedback (LLM revises)  [q]uit: ").lower()
        if c == "a":
            st.s["status"] = "beats_review"
            st.hitl(ep=0, type="plan_approved")
            st.save()
        elif c == "e":
            st.s["plan"] = edit_json(st.s["plan"])
            st.hitl(ep=0, type="plan_edit")
            st.save()
        elif c == "f":
            st.revise_plan(ask("Feedback on the plan: "))
        elif c == "q":
            return False
    if st.s["status"] == "beats_review":
        print(f"Expanding the arc into {TOTAL} episode beats... (live view: {st.dir / 'arc_plan.md'})")
        export(st)
        st.expand_beats(progress=lambda m: (print(m), export(st)))  # refresh arc_plan.md after every act
        export(st)
        while True:
            c = ask(f"Beats written to {st.dir / 'arc_plan.md'}. [a]pprove & start writing  [e]dit beats JSON  [q]uit: ").lower()
            if c == "a":
                st.s["status"] = "writing"
                st.save()
                break
            if c == "e":
                st.s["beats"] = edit_json(st.s["beats"])
                st.hitl(ep=0, type="beats_edit")
                st.save()
                export(st)
            if c == "q":
                return False
    return True


# ---------------- episode loop (HITL gate #2) ----------------
def show(st, n):
    title, text = st.read_ep(n)
    e = st.s["episodes"][str(n)]
    c = e.get("critic", {})
    print(f"\n{'=' * 70}\nEPISODE {n}/{TOTAL}: {title}\n{'=' * 70}\n{text}\n{'-' * 70}")
    print(f"words={e['words']}  revisions={e.get('revisions')}  cost=${e.get('cost', 0):.4f}  "
          f"hook={c.get('hook')} momentum={c.get('momentum')} prose={c.get('prose')}  "
          f"critic={'PASS' if c.get('passed') else 'ISSUES: ' + '; '.join(map(str, c.get('issues', [])))[:300]}"
          + (f"  (stopped: {c['stopped']})" if c.get("stopped") else ""))


def show_feedback(st, r, changed):
    h = st.s["hitl"][-1]
    pc = h.get("plan_changes", {})
    print(f"Directive: {r.get('directive') or '(none)'}\nBeats rewritten now: {changed}")
    print(f"Plan updated -> arcs: {pc.get('characters') or '-'} | threads: {pc.get('threads') or '-'} | acts: {pc.get('acts') or '-'}")
    print(f"Later beats marked stale (writer adapts; edit them if you like): {h.get('beats_stale', 0)}\nWhy: {r.get('rationale')}")


def write_loop(st, count, auto):
    done = 0
    while done < count and st.s["next_ep"] <= TOTAL:
        n = st.s["next_ep"]
        if st.s["episodes"].get(str(n), {}).get("status") != "draft":  # resume: reuse an unreviewed draft
            print(f"\nWriting episode {n}...")
            st.produce(n)
        while True:
            show(st, n)
            c = "a" if auto else ask("[a]pprove  [e]dit  [r]eject+note  [f]eedback (carries forward)  [v]iew critic  [q]uit: ").lower()
            if c == "a":
                st.approve(n)
                break
            if c == "e":
                open_editor(st.ep_path(n))
                st.approve(n, edited=True)
                break
            if c == "r":
                note = ask("What's wrong / what should change in this episode? ")
                st.hitl(ep=n, type="reject", text=note)
                st.produce(n, note=note)
            elif c == "f":
                fb = ask("Feedback for the rest of the story (e.g. 'slow down the romance'): ")
                r, changed = st.feedback(fb, n)
                show_feedback(st, r, changed)
                if ask("Regenerate this episode under the new direction? [y/n] ").lower() == "y":
                    st.produce(n)
            elif c == "v":
                print(json.dumps(st.s["episodes"][str(n)].get("critic"), indent=2, ensure_ascii=False))
            elif c == "q":
                print(f"Stopped. Resume any time with: {hint('resume', st.dir.name)}")
                return
        done += 1
        print(f"Approved ep {n}. Total spend so far: ${st.llm.spent():.3f}")


# ---------------- exports ----------------
def export(st):
    p, s = st.s["plan"], st.s
    L = [f"# {p['title']} - Arc Plan ({TOTAL} episodes)\n", f"**Premise:** {s['premise']}\n", f"**Logline:** {p['logline']}\n",
         f"**Tone:** {p['tone']}  \n**POV:** {p.get('pov')}  \n**Setting:** {p['setting']}\n", "## World rules"]
    L += [f"- {r}" for r in p["world_rules"]] + ["\n## Characters"]
    L += [f"- **{c['name']}** ({c['role']}): {c['desc']}  \n  Arc: {c['arc']}" + (f" _(revised by {c['revised_by']}; was: {c['arc_history'][0]})_" if c.get("revised_by") else "")
          + f"  \n  Secret: {c.get('secret', '')}" for c in p["characters"]]
    L += ["\n## Threads"] + [f"- **{t['id']}** {'~~' if t.get('dropped') else ''}{t['desc']}{'~~ DROPPED' if t.get('dropped') else ''} "
                              f"(opens by {t.get('opens_by')}, resolves by {t.get('resolves_by')})"
                              + (f" _(revised by {t['revised_by']})_" if t.get("revised_by") else "") for t in p["threads"]]
    if s["directives"]:
        L += ["\n## Showrunner directives (from human feedback)"] + [
            f"- **{d['id']}** (from ep {d['from_ep']}, {'active' if d['active'] else 'retired'}): {d['text']}  \n  _source: {d['source']}_" for d in s["directives"]]
    L += [f"\n## Ending\n{p['ending']}\n"]
    for a in p["acts"]:
        L.append(f"\n## Act {a['n']}: {a['title']} (eps {a['start']}-{a['end']})\n{a['summary']}  \n**Turning point:** {a['turning_point']}\n")
        for e in range(a["start"], a["end"] + 1):
            b = s["beats"].get(str(e), {})
            tag = (f" _(revised by {b['revised_by']})_" if b.get("revised_by") else "") + (f" **[STALE: planned before {b['stale'].split(':')[0]}]**" if b.get("stale") else "")
            L.append(f"{e}. {b.get('beat', '')} **Hook:** {b.get('hook', '')}{tag}")
    (st.dir / "arc_plan.md").write_text("\n".join(L) + "\n")
    eps = st.approved()
    (st.dir / "story.md").write_text(f"# {p['title']}\n\n" + "\n\n---\n\n".join(st.ep_path(k).read_text() for k in eps))
    H = ["# Human-in-the-loop log\n"]
    for h in s["hitl"]:
        H.append(f"- **ep {h.get('ep')} / {h['type']}**: {h.get('text', '')}"
                 + (f"\n  - directive: {h['directive']}" if h.get("directive") else "")
                 + (f"\n  - beats rewritten: {h['beats_changed']}" if h.get("beats_changed") else "")
                 + (f"\n  - conflicts: {h['conflicts']}" if h.get("conflicts") else ""))
    for x in s["beat_history"]:
        H.append(f"- beat {x['ep']} before feedback '{x['feedback']}': {x['old'].get('beat')}")
    (st.dir / "hitl_log.md").write_text("\n".join(H) + "\n")


def status(st):
    s = st.s
    flagged = {k: e["flag"] for k, e in s["episodes"].items() if e.get("flag")}
    m = st.memory(s["next_ep"])
    print(f"Run: {st.dir.name} | status: {s['status']} | next episode: {s['next_ep']}/{TOTAL} | approved: {len(st.approved())}")
    print(f"Spend: ${st.llm.spent():.3f} | chapters summarized: {len(s['chapters'])}")
    print("Directives:", *[f"\n  {d['id']} (from ep {d['from_ep']}): {d['text']}" for d in s["directives"] if d["active"]] or [" none"])
    print("Open threads:", *[f"\n  {k}: {v['desc']} (last ep {v['last']})" for k, v in m["threads"].items() if v["status"] == "open"] or [" none"])
    print("Dead/missing:", [c for c, v in m["chars"].items() if v["status"] != "alive"])
    if flagged:
        print("Flagged by retcon:", *[f"\n  ep {k}: {v}" for k, v in flagged.items()])


def main():
    ap = argparse.ArgumentParser(description="Agentic 200-episode serial writer with human-in-the-loop")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("new", help="plan a new serial from a premise");  a.add_argument("name"); a.add_argument("premise")
    a.add_argument("--plan-only", action="store_true", help="stop after the plan is approved (don't start writing)")
    a = sub.add_parser("arc", help="plan + review the arc and beats only (no writing)"); a.add_argument("name"); a.add_argument("premise")
    a = sub.add_parser("episode", help="write + review exactly the next episode"); a.add_argument("name")
    a.add_argument("n", nargs="?", type=int, default=1, help="how many episodes (default 1)")
    a = sub.add_parser("plan", help="resume arc/beat review");            a.add_argument("name")
    a = sub.add_parser("write", help="write + review episodes (resumes)"); a.add_argument("name")
    a.add_argument("--count", type=int, default=999); a.add_argument("--auto", action="store_true", help="auto-approve")
    a = sub.add_parser("feedback", help="standing feedback for future episodes"); a.add_argument("name"); a.add_argument("text")
    a = sub.add_parser("edit", help="edit a past episode (retcon)");        a.add_argument("name"); a.add_argument("ep", type=int)
    a = sub.add_parser("regen", help="regenerate a (flagged) episode");    a.add_argument("name"); a.add_argument("ep", type=int)
    a.add_argument("--note", default=None)
    a = sub.add_parser("directive-off", help="retire a directive");       a.add_argument("name"); a.add_argument("id")
    for c in ("status", "export", "estimate"):
        sub.add_parser(c).add_argument("name")
    args = ap.parse_args()
    log.info("command=%s args=%s", args.cmd, {k: v for k, v in vars(args).items() if k != "cmd"})
    if args.cmd == "arc":      # alias: new --plan-only
        args.cmd, args.plan_only = "new", True
    if args.cmd == "episode":  # alias: write --count n
        args.cmd, args.count, args.auto = "write", args.n, False

    if args.cmd == "new":
        st = Story(args.name)
        if st.s and st.s.get("plan", {}).get("title"):
            sys.exit(f"Run '{args.name}' exists. Continue with: {hint('resume', args.name)}")
        if st.s:
            print(f"Run '{args.name}' has no valid plan (an earlier attempt failed); re-planning.")
        print("Planning the arc...")
        st.new(args.premise)
        if review_plan(st):
            if args.plan_only:
                print(f"Arc approved. Next: open {st.dir / 'arc_plan.md'} to read it, then write episodes with: {hint('next', args.name)}")
            else:
                write_loop(st, 999, False)
        return
    st = load(args.name)
    if args.cmd in ("plan", "write") and st.s["status"] != "writing" and not review_plan(st):
        return
    if args.cmd == "write":
        write_loop(st, args.count, args.auto)
    elif args.cmd == "feedback":
        r, changed = st.feedback(args.text, st.s["next_ep"])
        show_feedback(st, r, changed)
    elif args.cmd == "edit":
        if args.ep >= st.s["next_ep"] or str(args.ep) not in st.s["episodes"]:
            sys.exit("Only approved episodes can be retconned; drafts are edited in the write loop.")
        open_editor(st.ep_path(args.ep))
        r = st.retcon(args.ep)
        print(f"Downstream conflicts: {r.get('conflicts')}\nContinuity note added: {r.get('continuity_note')}")
    elif args.cmd == "regen":
        st.produce(args.ep, note=args.note or st.s["episodes"].get(str(args.ep), {}).get("flag"))
        show(st, args.ep)
        if ask("[a]pprove replacement / [k]eep draft for later: ") == "a":
            st.s["episodes"][str(args.ep)].pop("flag", None)
            st.approve(args.ep)
    elif args.cmd == "directive-off":
        for d in st.s["directives"]:
            if d["id"] == args.id:
                d["active"] = False
        st.save()
    elif args.cmd == "status":
        status(st)
    elif args.cmd == "estimate":
        print(st.estimate())
    if args.cmd in ("write", "feedback", "edit", "regen", "export", "directive-off"):
        export(st)
        if args.cmd == "export":
            print(f"Wrote {st.dir}/arc_plan.md, story.md, hitl_log.md")


def run():
    """Entry point: every crash lands in logs/serial.log with file + line."""
    try:
        main()
    except KeyboardInterrupt:
        log.info("interrupted by user (state is saved)")
        sys.exit(130)
    except SystemExit as e:
        if e.code not in (0, None):
            log.error("exit: %s", e.code)
        raise
    except Exception as e:
        log.exception("unhandled error")
        sys.exit(f"Error: {type(e).__name__}: {e}\nDetails: {LOG_FILE}")


if __name__ == "__main__":
    run()
