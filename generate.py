"""Single entry point for the serial writer.

  uv run generate.py ui                                          # browser UI at http://localhost:8765 (same story/state)
  uv run generate.py --premise "A delivery rider realizes ..."   # plan the arc + threads + 200 beats, review them
  uv run generate.py plan                                        # re-open the arc/beats review
  uv run generate.py create 5                                    # write + review the NEXT 5 episodes (continues from the last one)
  uv run generate.py feedback "Kill off Mara within 3 episodes"  # standing note for all future episodes
  uv run generate.py edit 4                                      # edit an already-approved episode (retcon)
  uv run generate.py regen 13 --note "show the death on-page"   # rewrite one episode
  uv run generate.py status | estimate | export                  # where the story stands / cost / write .md files
  uv run generate.py off D2                                      # switch off standing instruction D2

  --run NAME    work on another story (default: story)
  --fresh       with --premise: start over (old run is kept as runs/NAME.bak-<time>)
While reviewing an episode: a=approve  e=edit  r=reject+note  f=feedback  v=critic report  q=quit
"""
import argparse, os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)


def cli():
    ap = argparse.ArgumentParser(prog="generate.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--premise", help="plan a new story from this premise")
    ap.add_argument("--run", default="story", help="story name (default: story)")
    ap.add_argument("--fresh", action="store_true", help="with --premise: replace an existing story")
    ap.add_argument("action", nargs="?", choices=["ui", "plan", "create", "feedback", "edit", "regen", "status",
                                                    "estimate", "export", "off"])
    ap.add_argument("value", nargs="?", help="episodes to create | feedback text | episode number | directive id")
    ap.add_argument("--note", help="with regen: what to change")
    ap.add_argument("--auto", action="store_true", help="with create: approve without review")
    a = ap.parse_args()

    run_dir = os.path.join(os.getenv("RUNS_DIR", "runs"), a.run)
    if a.premise:
        if os.path.exists(os.path.join(run_dir, "state.json")):
            if not a.fresh:
                sys.exit(f"Story '{a.run}' already exists. Continue with `uv run generate.py create 5`, "
                         f"or start over with `uv run generate.py --premise \"...\" --fresh` (or pick another --run).")
            os.rename(run_dir, f"{run_dir}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            print(f"Old story kept as {run_dir}.bak-*")
        return ["arc", a.run, a.premise]
    if not a.action:
        ap.print_help()
        sys.exit(0)
    if a.action == "ui":
        import ui  # browser UI on http://localhost:8765
        ui.serve()
        sys.exit(0)
    need = {"feedback": "the feedback text", "edit": "an episode number", "regen": "an episode number",
            "off": "a directive id like D2"}
    if a.action in need and not a.value:
        sys.exit(f"`{a.action}` needs {need[a.action]}. Example: uv run generate.py {a.action} "
                 + {"feedback": '"slow down the romance"', "edit": "4", "regen": "13", "off": "D2"}[a.action])
    if a.action == "create":
        n = a.value or "1"
        if not n.isdigit() or int(n) < 1:
            sys.exit("Usage: uv run generate.py create <number of episodes>")
        return ["write", a.run, "--count", n] + (["--auto"] if a.auto else [])
    if a.action == "regen":
        return ["regen", a.run, a.value] + (["--note", a.note] if a.note else [])
    if a.action == "off":
        return ["directive-off", a.run, a.value]
    if a.action in ("feedback", "edit"):
        return [a.action, a.run, a.value]
    return [a.action, a.run]


if __name__ == "__main__":
    args = cli()
    sys.argv = ["generate.py"] + args
    import main  # loads .env, logging, engine
    main.run()
