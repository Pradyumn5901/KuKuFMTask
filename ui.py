"""Browser UI for the serial writer: vanilla HTML/JS + Python stdlib server (no extra dependencies).

  uv run generate.py ui            # or: uv run ui.py      -> http://localhost:8765

Same engine and the same runs/<name>/state.json as the CLI, so you can switch between them at any time.
Long LLM steps run in a background job; the page polls for progress and shows the live log.
"""
import contextlib, io, json, os, sys, threading, time, traceback, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
import main as cli  # noqa: E402  (loads .env + logging before engine settings are read)
import engine  # noqa: E402
from engine import Story, TOTAL, DEAD  # noqa: E402

RUNS = Path(os.getenv("RUNS_DIR", "runs"))
JOB = {"id": 0, "running": False, "label": "", "log": [], "error": None, "result": None, "started": 0, "ended": 0}
LOCK = threading.Lock()


# ---------------- background jobs (one at a time; LLM calls can take minutes) ----------------
class _Tee(io.TextIOBase):
    def write(self, s):
        sys.__stdout__.write(s)
        for line in s.splitlines():
            if line.strip():
                JOB["log"].append(line.strip()[-400:])
                del JOB["log"][:-200]
        return len(s)


def start_job(label, fn):
    with LOCK:
        if JOB["running"]:
            return False
        JOB.update(id=JOB["id"] + 1, running=True, label=label, log=[], error=None, result=None,
                   started=time.time(), ended=0)

    def work():
        try:
            with contextlib.redirect_stdout(_Tee()):
                JOB["result"] = fn()
        except SystemExit as e:
            JOB["error"] = str(e.code)
        except Exception as e:  # noqa: BLE001
            cli.log.exception("ui job failed: %s", label)
            JOB["error"] = f"{type(e).__name__}: {e}  (details: logs/serial.log)"
        finally:
            JOB.update(running=False, ended=time.time())

    threading.Thread(target=work, daemon=True).start()
    return True


# ---------------- read side ----------------
def runs():
    return sorted(p.name for p in RUNS.glob("*") if (p / "state.json").exists() and ".bak-" not in p.name)


def state(run):
    st = Story(run)
    base = {"run": run, "runs": runs(), "total": TOTAL, "mock": os.getenv("LLM_PROVIDER") == "mock",
            "models": {"planner": engine.PLANNER, "writer": engine.WRITER, "critic": engine.CRITIC,
                       "extract": engine.CHEAP, "fallback": engine.FALLBACK}}
    if not st.s:
        return {**base, "exists": False}
    S, n = st.s, st.s["next_ep"]
    eps = []
    for k in sorted(S["episodes"], key=int):
        e = S["episodes"][k]
        eps.append({"n": int(k), "title": e.get("title", ""), "status": e.get("status"), "words": e.get("words"),
                    "edited": e.get("edited", False), "flag": e.get("flag"),
                    "summary": e.get("delta", {}).get("summary", ""), "hook": e.get("delta", {}).get("hook", "")})
    draft = None
    if S["episodes"].get(str(n), {}).get("status") == "draft":
        t, x = st.read_ep(n)
        draft = {"n": n, "title": t, "text": x, **{k: S["episodes"][str(n)].get(k) for k in ("critic", "revisions", "cost", "words")}}
    mem = None
    if S["status"] == "writing":
        m = st.memory(n)
        mem = {"cast": [{"name": c, **{k: v[k] for k in ("status", "role", "last_seen")}, "recent": v["notes"][-2:]}
                        for c, v in m["chars"].items()],
               "threads": [{"id": t, **v} for t, v in m["threads"].items()],
               "rels": m["rels"], "facts": m["facts"][-60:], "timeline": m["timeline"]}
    return {**base, "exists": True, "premise": S["premise"], "status": S["status"], "plan": S["plan"],
            "beats": [S["beats"][k] | {"ep": int(k)} for k in sorted(S["beats"], key=int)],
            "next_ep": n, "draft": draft, "episodes": eps, "directives": S["directives"], "hitl": S["hitl"],
            "beat_history": S["beat_history"][-40:], "chapters": S["chapters"], "memory": mem,
            "spend": round(st.llm.spent(), 4)}


def trace(run):
    st = Story(run)
    rows = [json.loads(l) for l in st.llm.trace.read_text().splitlines()] if st.llm.trace.exists() else []
    agg = {}
    for r in rows:
        if "in_tok" in r:
            a = agg.setdefault((r["step"], r.get("model", "")), {"step": r["step"], "model": r.get("model", ""), "calls": 0,
                                                                 "in_tok": 0, "out_tok": 0, "cost": 0.0, "latency": 0.0})
            a["calls"] += 1; a["in_tok"] += r["in_tok"]; a["out_tok"] += r["out_tok"]
            a["cost"] += r.get("cost", 0); a["latency"] += r.get("latency", 0)
    events = [r for r in rows if "event" in r or r.get("step") in ("decision", "hitl")][-60:]
    log = Path(cli.LOG_FILE)
    return {"agg": sorted(agg.values(), key=lambda a: -a["cost"]), "events": events[::-1],
            "estimate": st.estimate() if st.s else "", "log": log.read_text().splitlines()[-80:] if log.exists() else [],
            "totals": {"calls": sum(a["calls"] for a in agg.values()), "tokens": sum(a["in_tok"] + a["out_tok"] for a in agg.values()),
                       "cost": round(sum(a["cost"] for a in agg.values()), 4)}}


# ---------------- write side ----------------
def action(run, a, d):
    """Instant actions return a dict; LLM actions start a background job."""
    st = Story(run)
    S = st.s
    n = int(d.get("n") or (S or {}).get("next_ep") or 1)

    if a == "plan_approve":
        S["status"] = "beats_review"; st.hitl(ep=0, type="plan_approved"); st.save(); return {"ok": True}
    if a == "plan_save":
        S["plan"] = d["plan"]; S["beats"] = {}; st.hitl(ep=0, type="plan_edit"); st.save(); return {"ok": True}
    if a == "beats_save":
        S["beats"] = {str(b["ep"]): {k: v for k, v in b.items()} for b in d["beats"]}
        st.hitl(ep=0, type="beats_edit"); st.save(); cli.export(st); return {"ok": True}
    if a == "beats_approve":
        S["status"] = "writing"; st.save(); cli.export(st); return {"ok": True}
    if a == "directive_off":
        for x in S["directives"]:
            if x["id"] == d["id"]:
                x["active"] = False
        st.hitl(ep=S["next_ep"], type="directive_off", text=d["id"]); st.save(); return {"ok": True}
    if a == "export":
        cli.export(st); return {"ok": True}

    def job(label, fn):
        if not start_job(label, fn):
            return {"ok": False, "error": "Another step is still running."}
        return {"ok": True, "job": JOB["id"]}

    def fresh(s):
        return Story(run)

    if a == "new":
        def f():
            s = Story(run)
            if s.s and s.s.get("plan", {}).get("title"):
                if not d.get("fresh"):
                    raise SystemExit(f"Story '{run}' already exists. Tick 'replace existing' or use another name.")
                os.rename(s.dir, f"{s.dir}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
                s = Story(run)
            print("Planning the arc...")
            s.new(d["premise"].strip())
            return "Plan ready for review"
        return job("Planning the arc", f)
    if a == "plan_feedback":
        return job("Revising the plan", lambda: (fresh(0).revise_plan(d["note"]), "Plan revised")[1])
    if a == "beats_generate":
        def f():
            s = fresh(0); s.expand_beats(progress=print); cli.export(s); return "Beats ready"
        return job(f"Writing {TOTAL} episode beats", f)
    if a == "write":
        return job(f"Writing episode {n}", lambda: (fresh(0).produce(n), f"Episode {n} drafted")[1])
    if a == "approve":
        def f():
            s = fresh(0); s.approve(n); cli.export(s); return f"Episode {n} approved"
        return job(f"Approving episode {n} (updating memory)", f)
    if a == "approve_next":
        def f():
            s = fresh(0); s.approve(n); cli.export(s)
            if n < TOTAL:
                print(f"Writing episode {n + 1}..."); fresh(0).produce(n + 1)
            return f"Episode {n} approved, episode {n + 1} drafted"
        return job(f"Approving {n} and writing {n + 1}", f)
    if a == "reject":
        def f():
            s = fresh(0); s.hitl(ep=n, type="reject", text=d["note"]); s.save(); s.produce(n, note=d["note"])
            return f"Episode {n} rewritten from your note"
        return job(f"Rewriting episode {n} from your note", f)
    if a == "edit_approve":
        def f():
            s = fresh(0); s.write_ep(n, d["title"], d["text"]); s.approve(n, edited=True); cli.export(s)
            return f"Your edit of episode {n} approved"
        return job(f"Saving your edit of episode {n}", f)
    if a == "feedback":
        def f():
            s = fresh(0); s.feedback(d["text"], n)
            if d.get("regen"):
                print(f"Regenerating episode {n} under the new direction..."); fresh(0).produce(n)
            cli.export(fresh(0))
            return "Feedback applied"
        return job("Applying feedback to plan, beats and rules", f)
    if a == "retcon":
        def f():
            s = fresh(0); t, _ = s.read_ep(n); s.write_ep(n, d.get("title") or t, d["text"]); r = s.retcon(n)
            cli.export(fresh(0)); return r
        return job(f"Retcon of episode {n}: rebuilding memory, checking later episodes", f)
    if a == "regen":
        return job(f"Regenerating episode {n}", lambda: (fresh(0).produce(n, note=d.get("note") or None), f"Episode {n} redrafted")[1])
    if a == "approve_past":
        def f():
            s = fresh(0); s.s["episodes"][str(n)].pop("flag", None); s.approve(n); cli.export(s)
            return f"Episode {n} re-approved"
        return job(f"Re-approving episode {n}", f)
    return {"ok": False, "error": f"unknown action {a}"}


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        run = q.get("run", "story")
        try:
            if u.path == "/":
                return self._send(200, PAGE, "text/html")
            if u.path == "/api/state":
                return self._send(200, state(run))
            if u.path == "/api/job":
                return self._send(200, {k: JOB[k] for k in JOB if k != "log"} | {"log": JOB["log"][-12:]})
            if u.path == "/api/trace":
                return self._send(200, trace(run))
            if u.path == "/api/brief":
                st = Story(run)
                return self._send(200, {"brief": st.context(st.s["next_ep"]) if st.s and st.s["status"] == "writing" else ""})
            if u.path == "/api/episode":
                st = Story(run)
                k = int(q["n"]); t, x = st.read_ep(k)
                e = st.s["episodes"].get(str(k), {})
                return self._send(200, {"n": k, "title": t, "text": x, "critic": e.get("critic"), "delta": e.get("delta"),
                                        "status": e.get("status"), "flag": e.get("flag")})
            if u.path == "/files":
                p = RUNS / run / Path(q["name"]).name
                if p.exists():
                    return self._send(200, p.read_bytes(), "text/markdown")
            return self._send(404, {"error": "not found"})
        except Exception as e:  # noqa: BLE001
            cli.log.exception("ui GET %s", u.path)
            return self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def do_POST(self):
        try:
            d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            return self._send(200, action(d.get("run", "story"), d["action"], d))
        except SystemExit as e:
            return self._send(200, {"ok": False, "error": str(e.code)})
        except Exception as e:  # noqa: BLE001
            cli.log.exception("ui POST")
            return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def serve():
    port = int(os.getenv("UI_PORT", "8765"))
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://localhost:{port}"
    print(f"Serial UI running at {url}   (Ctrl+C to stop)")
    if os.getenv("UI_NO_BROWSER") != "1":
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>Serial Writer</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--panel2:#1e222b;--line:#2a2f3a;--txt:#e6e8ee;--mute:#9aa3b2;--acc:#7aa2ff;--ok:#4cc38a;--warn:#f0b429;--bad:#ef5b5b}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,BlinkMacSystemFont,Segoe UI,Inter,sans-serif;background:var(--bg);color:var(--txt)}
header{display:flex;flex-wrap:wrap;gap:10px 16px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:5}
header h1{font-size:16px;margin:0;font-weight:650;white-space:nowrap}.chip{white-space:nowrap}
@media(max-width:900px){header h1 .mute{display:none}}header .sp{flex:1}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);font-size:12px;color:var(--mute);margin:1px}
.chip.ok{color:var(--ok);border-color:#2d5a45}.chip.warn{color:var(--warn);border-color:#5a4a1d}.chip.bad{color:var(--bad);border-color:#5a2d2d}.chip.acc{color:var(--acc);border-color:#334a7a}
nav{display:flex;gap:4px;padding:10px 20px 0;border-bottom:1px solid var(--line);background:var(--panel)}
nav{overflow-x:auto}nav button{white-space:nowrap;background:none;border:none;color:var(--mute);padding:9px 14px;border-bottom:2px solid transparent;cursor:pointer;font-size:14px}
nav button.on{color:var(--txt);border-color:var(--acc)}
main{padding:18px 20px;max-width:1200px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:14px}
.card h2{font-size:15px;margin:0 0 10px}.card h3{font-size:13px;margin:14px 0 6px;color:var(--mute);text-transform:uppercase;letter-spacing:.04em}
button.b{background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:7px 13px;cursor:pointer;font-size:13px;margin:3px 4px 3px 0}
button.b:hover{border-color:var(--acc)}button.p{background:var(--acc);color:#0b1020;border-color:var(--acc);font-weight:600}
button.g{background:var(--ok);color:#06140d;border-color:var(--ok);font-weight:600}button.r{border-color:#5a2d2d;color:#ffb4b4}
button:disabled{opacity:.45;cursor:not-allowed}
textarea,input[type=text],select{width:100%;background:var(--panel2);color:var(--txt);border:1px solid var(--line);border-radius:7px;padding:8px 10px;font:inherit}
textarea{min-height:70px;resize:vertical}textarea.big{min-height:380px;font:13px/1.55 ui-monospace,Menlo,monospace}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mute);font-weight:500;font-size:12px}tr.dead td{color:var(--bad)}tr.stale td{background:#2a2410}
.story{white-space:pre-wrap;font:15px/1.7 Georgia,serif;max-height:520px;overflow:auto;background:var(--panel2);padding:16px 18px;border-radius:8px}
.mute{color:var(--mute)}.row{display:flex;gap:12px;flex-wrap:wrap}.row>*{flex:1;min-width:240px}
.metrics{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 10px}.m{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:6px 12px}
.m b{display:block;font-size:17px}.m span{font-size:11px;color:var(--mute)}
#job{display:none;position:sticky;top:0;z-index:4;background:#13213d;border-bottom:1px solid #2b4a85;padding:10px 20px}
#job pre{margin:6px 0 0;max-height:120px;overflow:auto;font-size:12px;color:#b9c8e8}
.spin{display:inline-block;width:12px;height:12px;border:2px solid #7aa2ff55;border-top-color:var(--acc);border-radius:50%;animation:s 1s linear infinite;margin-right:8px;vertical-align:-1px}
@keyframes s{to{transform:rotate(360deg)}}
#toast{position:fixed;right:18px;bottom:18px;max-width:520px;padding:12px 16px;border-radius:8px;display:none;z-index:9}
pre.code{white-space:pre-wrap;background:var(--panel2);padding:12px;border-radius:8px;font-size:12px;max-height:520px;overflow:auto}
details summary{cursor:pointer;color:var(--acc);margin:6px 0}.issue{color:var(--warn)}.hide{display:none}
label.ck{display:inline-flex;gap:6px;align-items:center;color:var(--mute);font-size:13px;margin:6px 10px 6px 0}
</style></head><body>
<header><h1>📦 Serial Writer <span class="mute" style="font-weight:400">· 200-episode serial, human in the loop</span></h1>
<span class="sp"></span><span id="hdr"></span>
<input id="run" type="text" list="runlist" style="width:150px" title="story name"><datalist id="runlist"></datalist>
<button class="b" onclick="setRun()">Open</button></header>
<div id="job"><span class="spin"></span><b id="jlabel"></b> <span id="jtime" class="mute"></span><pre id="jlog"></pre></div>
<nav id="tabs"></nav><main id="main"></main><div id="toast"></div>
<script>
const TABS=[["plan","1 · Plan"],["episodes","2 · Episodes"],["feedback","3 · Feedback"],["memory","4 · Memory"],["retcon","5 · Retcon"],["trace","6 · Trace & cost"]];
let RUN=localStorage.getItem("run")||"story", TAB=location.hash.slice(1)||"plan", S=null, JOBID=null, BUSY=false, UI={};
const $=s=>document.querySelector(s), esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const chip=(t,c="")=>`<span class="chip ${c}">${esc(t)}</span>`;
async function get(p){const r=await fetch(p+(p.includes("?")?"&":"?")+"run="+encodeURIComponent(RUN));return r.json()}
async function post(action,d={}){const r=await (await fetch("/api/action",{method:"POST",body:JSON.stringify({run:RUN,action,...d})})).json();
  if(!r.ok){toast(r.error||"failed",1);return r} if(r.job){JOBID=r.job;BUSY=true;pollJob()} else await load(); return r}
function toast(t,bad){const e=$("#toast");e.textContent=t;e.style.display="block";e.style.background=bad?"#4a1d1d":"#163a2a";e.style.border="1px solid "+(bad?"#ef5b5b":"#4cc38a");clearTimeout(e._t);e._t=setTimeout(()=>e.style.display="none",bad?9000:3500)}
function setRun(){RUN=$("#run").value.trim()||"story";localStorage.setItem("run",RUN);load()}
function go(t){TAB=t;location.hash=t;render()}
async function pollJob(){const j=await (await fetch("/api/job")).json();const box=$("#job");
  if(j.running){BUSY=true;box.style.display="block";$("#jlabel").textContent=j.label+"…";$("#jtime").textContent=Math.round(Date.now()/1000-j.started)+"s";
    $("#jlog").textContent=(j.log||[]).join("\n");$("#jlog").scrollTop=1e9;disable();setTimeout(pollJob,1200);return}
  box.style.display="none";if(BUSY){BUSY=false;if(j.error)toast(j.error,1);else if(j.result)toast(typeof j.result=="string"?j.result:"Done");
    if(j.result&&j.result.conflicts!==undefined)UI.retcon=j.result;await load()}}
async function load(){S=await get("/api/state");$("#run").value=RUN;$("#runlist").innerHTML=(S.runs||[]).map(r=>`<option value="${esc(r)}">`).join("");render()}
function render(keep){
  $("#tabs").innerHTML=TABS.map(([k,v])=>`<button class="${k==TAB?"on":""}" onclick="go('${k}')">${v}</button>`).join("");
  const h=S?.exists?[chip(S.status,S.status=="writing"?"ok":"warn"),chip(`next ep ${S.next_ep}/${S.total}`,"acc"),chip(`approved ${S.episodes.filter(e=>e.status=="approved").length}`),chip(`$${S.spend} list price`)]:[];
  if(S?.mock)h.push(chip("MOCK MODEL","bad"));$("#hdr").innerHTML=h.join("");
  if(keep&&document.activeElement&&["TEXTAREA","INPUT"].includes(document.activeElement.tagName))return disable();
  $("#main").innerHTML=S?(V[TAB]||V.plan)():"Loading…";disable();if(TAB=="trace")loadTrace();}
function disable(){document.querySelectorAll("button.b[data-llm]").forEach(b=>b.disabled=BUSY)}
const val=id=>($("#"+id)?.value||"").trim();
function table(rows,cols,cls){if(!rows.length)return'<p class="mute">none</p>';return`<table><tr>${cols.map(c=>`<th>${c[1]}</th>`).join("")}</tr>${rows.map(r=>`<tr class="${cls?cls(r):""}">${cols.map(c=>`<td>${c[2]?c2:esc(r[c[0]])}</td>`).join("")}</tr>`).join("")}</table>`}
const BTN=(label,on,cls="",llm=1)=>`<button class="b ${cls}" ${llm?"data-llm":""} onclick="${on}">${label}</button>`;

function planView(p){return`<div class="card"><h2>${esc(p.title)}</h2><p>${esc(p.logline)}</p>
 <div class="row"><div><h3>Tone</h3>${esc(p.tone)}</div><div><h3>POV</h3>${esc(p.pov)}</div><div><h3>Setting</h3>${esc(p.setting)}</div></div>
 <h3>World rules (never contradicted)</h3><ul>${(p.world_rules||[]).map(r=>`<li>${esc(r)}</li>`).join("")}</ul>
 <h3>Characters & arcs</h3>${table(p.characters,[["name","Name"],["role","Role"],["arc","Arc",c=>esc(c.arc)+(c.revised_by?`<br>${chip("revised by "+c.revised_by,"warn")}`:"")],["secret","Secret"]])}
 <h3>Acts & turning points</h3>${table(p.acts,[["n","#"],["title","Act"],["eps","Episodes",a=>a.start+"–"+a.end],["summary","Summary"],["turning_point","Turning point",a=>esc(a.turning_point)+(a.revised_by?`<br>${chip("revised","warn")}`:"")]])}
 <h3>Threads (open-by / resolve-by targets)</h3>${table(p.threads,[["id","ID"],["desc","Thread",t=>t.dropped?`<s>${esc(t.desc)}</s> ${chip("dropped","bad")}`:esc(t.desc)+(t.revised_by?" "+chip("revised","warn"):"")],["opens_by","Opens by"],["resolves_by","Resolves by"]])}
 <h3>Ending</h3><p>${esc(p.ending)}</p></div>`}
function beatsView(open){const b=S.beats;return`<div class="card"><details ${open?"open":""}><summary>All ${b.length} episode beats (${b.filter(x=>x.revised_by).length} revised by feedback, ${b.filter(x=>x.stale).length} marked stale)</summary>
 ${table(b,[["ep","Ep"],["beat","Beat",x=>esc(x.beat)+(x.revised_by?` ${chip("revised · "+x.revised_by,"warn")}`:"")+(x.stale?` ${chip("STALE · planned before "+x.stale.split(":")[0],"bad")}`:"")],["hook","Hook"],["chars","On-page",x=>esc((x.chars||[]).join(", "))]],x=>x.stale?"stale":"")}</details></div>`}

const V={
plan(){if(!S.exists)return`<div class="card"><h2>New story</h2><p class="mute">One-line premise → the system plans a ${S.total}-episode arc (acts, turning points, character arcs, threads). You review it before anything is written.</p>
  <textarea id="premise" placeholder="A delivery rider realizes every address on today's route belongs to someone who died in the same building."></textarea>
  ${BTN("Create the arc plan","if(val('premise'))post('new',{premise:val('premise')})","p")}</div>`;
 let top=`<div class="card"><span class="mute">Premise:</span> ${esc(S.premise)}</div>`, ctl="";
 if(S.status=="plan_review")ctl=`<div class="card"><h2>Review the arc (human gate #1)</h2>
   ${BTN("✅ Approve arc","post('plan_approve')","g",0)}
   <h3>Ask the planner to revise</h3><textarea id="pfb" placeholder="e.g. Keep it grounded: present day, ONE building, each address is a flat whose tenant died there."></textarea>
   ${BTN("Revise plan with this note","if(val('pfb'))post('plan_feedback',{note:val('pfb')})")}
   <details><summary>Edit the plan JSON yourself</summary><textarea id="pjson" class="big">${esc(JSON.stringify(S.plan,null,2))}</textarea>
   ${BTN("Save plan","try{post('plan_save',{plan:JSON.parse($('#pjson').value)})}catch(e){toast('Invalid JSON: '+e.message,1)}","",0)}</details></div>`;
 if(S.status=="beats_review")ctl=S.beats.length<S.total?`<div class="card"><h2>Arc approved ✓</h2><p class="mute">Next: break the arc into one beat per episode (${S.total} beats, 10 per call).</p>${BTN(`Generate ${S.total} episode beats`,"post('beats_generate')","p")}${S.beats.length?`<span class="mute"> (${S.beats.length} done; resumes)</span>`:""}</div>`
   :`<div class="card"><h2>Review the beats</h2>${BTN("✅ Approve beats & start writing","post('beats_approve')","g",0)}
   <details><summary>Edit beats JSON yourself</summary><textarea id="bjson" class="big">${esc(JSON.stringify(S.beats,null,1))}</textarea>${BTN("Save beats","try{post('beats_save',{beats:JSON.parse($('#bjson').value)})}catch(e){toast('Invalid JSON: '+e.message,1)}","",0)}</details></div>`;
 if(S.status=="writing")ctl=`<div class="card">${chip("plan approved","ok")} ${chip("beats approved","ok")} <span class="mute">→ go to <a href="#episodes" onclick="go('episodes')" style="color:var(--acc)">Episodes</a></span>
   <details><summary>Start a different story</summary><input id="nrun" type="text" placeholder="new story name, e.g. story2"><textarea id="npremise" placeholder="premise"></textarea>
   ${BTN("Create","if(val('nrun')&&val('npremise')){RUN=val('nrun');localStorage.setItem('run',RUN);post('new',{premise:val('npremise')})}")}</details></div>`;
 return top+ctl+planView(S.plan)+(S.beats.length?beatsView(S.status=="beats_review"):"")},

episodes(){if(!S.exists||S.status!="writing")return`<div class="card">Approve the arc and the beats first (Plan tab).</div>`;
 const n=S.next_ep,b=S.beats[n-1]||{},d=S.draft;let out=`<div class="card"><h2>Episode ${n} of ${S.total}</h2>
  <div class="mute"><b>Planned beat:</b> ${esc(b.beat)} <b>Hook:</b> ${esc(b.hook)}</div>${b.stale?`<p>${chip("STALE beat · "+b.stale,"bad")} <span class="mute">the writer is told to follow current rules over this beat</span></p>`:""}`;
 if(!d)return out+`<p>${BTN(`✍️ Write episode ${n}`,"post('write')","p")} <span class="mute">writer → critic → up to 2 revisions → you</span></p></div>`+approvedList();
 const c=d.critic||{};out+=`<div class="metrics">${[["words",d.words],["revisions",d.revisions],["hook",c.hook],["momentum",c.momentum],["prose",c.prose],["cost $",d.cost]].map(([k,v])=>`<div class="m"><b>${esc(v)}</b><span>${k}</span></div>`).join("")}
  <div class="m"><b style="color:${c.passed?"var(--ok)":"var(--warn)"}">${c.passed?"PASS":"ISSUES"}</b><span>critic${c.stopped?" · stopped: "+c.stopped:""}</span></div></div>
  ${(c.issues||[]).length?`<div class="issue">${c.issues.map(i=>"• "+esc(typeof i=="string"?i:Object.values(i||{}).join(" | "))).join("<br>")}</div>`:""}
  <h2 style="margin-top:14px">${esc(d.title)}</h2><div class="story">${esc(d.text)}</div></div>
  <div class="card"><h2>Your decision (human gate #2)</h2>
  ${BTN("✅ Approve","post('approve',{n:"+n+"})","g")}${BTN("✅ Approve & write next","post('approve_next',{n:"+n+"})","p")}
  <div class="row" style="margin-top:10px">
   <div><h3>Reject with a note</h3><textarea id="rnote" placeholder="e.g. Too much explaining. Open mid-action, end on a concrete threat."></textarea>${BTN("↻ Reject & rewrite","if(val('rnote'))post('reject',{n:"+n+",note:val('rnote')})","r")}</div>
   <div><h3>Feedback for the rest of the story</h3><textarea id="efb" placeholder="e.g. Kill off Mara within 3 episodes; it must look like an accident. She stays dead."></textarea>
    <label class="ck"><input type="checkbox" id="eregen" checked> also rewrite this episode</label>${BTN("Apply feedback","if(val('efb'))post('feedback',{n:"+n+",text:val('efb'),regen:$('#eregen').checked})")}</div></div>
  <details ${UI.edit?"open":""} ontoggle="UI.edit=this.open"><summary>✏️ Edit this episode yourself</summary><input id="etitle" type="text" value="${esc(d.title)}"><textarea id="etext" class="big">${esc(d.text)}</textarea>
   ${BTN("Save my edit & approve","post('edit_approve',{n:"+n+",title:val('etitle'),text:$('#etext').value})","g")}</details>
  <details><summary>Critic report (JSON)</summary><pre class="code">${esc(JSON.stringify(c,null,2))}</pre></details></div>`;
 return out+approvedList()},

feedback(){if(!S.exists||S.status!="writing")return`<div class="card">Feedback is for the writing phase. (Plan notes go in the Plan tab.)</div>`;
 const fb=S.hitl.filter(h=>h.type=="feedback").reverse();
 return`<div class="card"><h2>Give feedback that carries forward</h2><p class="mute">Becomes (a) a standing rule the writer must follow and the critic enforces, (b) updates to character arcs, threads and acts, (c) rewrites of the next 25 beats, (d) later beats that mention affected characters are marked stale.</p>
  <textarea id="fb" placeholder="e.g. Slow down the romance: no confession or kiss before episode 60, only small gestures."></textarea>${BTN("Apply feedback (from episode "+S.next_ep+")","if(val('fb'))post('feedback',{text:val('fb')})","p")}</div>
  <div class="card"><h2>Standing rules (directives)</h2>${table(S.directives,[["id","ID"],["from_ep","From ep"],["text","Rule"],["source","Your words"],["active","",x=>x.active?BTN("turn off",`post('directive_off',{id:'${x.id}'})`,"",0):chip("off")]])}</div>
  <div class="card"><h2>What each feedback changed</h2>${fb.length?fb.map(h=>`<p><b>ep ${h.ep}:</b> “${esc(h.text)}”<br>${chip("directive","acc")} ${esc(h.directive||"—")}<br>
   ${chip("beats rewritten: "+(h.beats_changed||[]).length,"warn")} ${chip("arcs: "+((h.plan_changes||{}).characters||[]).join(", ")||"arcs: –")} ${chip("threads: "+(((h.plan_changes||{}).threads||[]).join(", ")||"–"))} ${chip("acts: "+(((h.plan_changes||{}).acts||[]).join(", ")||"–"))} ${chip("later beats marked stale: "+(h.beats_stale||0),"bad")}</p>`).join(""):'<p class="mute">none yet</p>'}</div>
  <div class="card"><details><summary>Beats before → after (${S.beat_history.length})</summary>${table(S.beat_history.slice().reverse(),[["ep","Ep"],["old","Before",x=>esc(x.old.beat)],["now","After",x=>esc((S.beats[x.ep-1]||{}).beat)],["feedback","Because of"]])}</details></div>`},

memory(){if(!S.memory)return`<div class="card">Memory starts once writing begins.</div>`;const m=S.memory;
 return`<div class="card"><h2>Cast (derived from approved episodes)</h2>${table(m.cast,[["name","Name"],["status","Status",x=>chip(x.status,DEADS.includes(x.status)?"bad":x.status=="alive"?"ok":"warn")],["role","Role"],["last_seen","Last seen ep"],["recent","Recent changes",x=>esc((x.recent||[]).join(" · "))]],x=>DEADS.includes(x.status)?"dead":"")}</div>
  <div class="card"><h2>Threads</h2>${table(m.threads,[["id","ID"],["desc","Thread"],["status","Status",x=>chip(x.status,x.status=="open"?"acc":x.status=="closed"?"ok":"")],["opened","Opened"],["last","Last touched",x=>esc(x.last)+(x.status=="open"&&S.next_ep-(x.last||S.next_ep)>15?" "+chip("STALE","warn"):"")],["resolves_by","Resolve by",x=>esc(x.resolves_by)+(x.status=="open"&&x.resolves_by&&x.resolves_by<S.next_ep?" "+chip("OVERDUE","bad"):"")]])}</div>
  <div class="row"><div class="card"><h2>Relationships</h2>${table(Object.entries(m.rels).map(([k,v])=>({k,v})),[["k","Who",x=>esc(x.k.replace("|"," & "))],["v","State"]])}</div>
   <div class="card"><h2>Timeline</h2><p>${esc(m.timeline||"–")}</p><h2>Chapter summaries</h2>${Object.entries(S.chapters).map(([k,v])=>`<p><b>Eps ${k}</b> ${esc(v)}</p>`).join("")||'<p class="mute">every 10 episodes</p>'}</div></div>
  <div class="card"><h2>Established facts (${m.facts.length} most recent)</h2>${table(m.facts.slice().reverse(),[["ep","Ep"],["f","Fact"]])}</div>
  <div class="card"><h2>What the writer sees for episode ${S.next_ep}</h2><p class="mute">Layered memory: bible → directives → chapter summaries → recent episodes → cast → threads → retrieved facts → used plot moves → beat.</p>
   ${BTN("Show the writer's brief","showBrief()","",0)}<pre class="code" id="brief" style="display:none"></pre></div>`},

retcon(){if(!S.exists||S.status!="writing")return`<div class="card">Nothing to retcon yet.</div>`;
 const ap=S.episodes.filter(e=>e.status=="approved"&&e.n<S.next_ep),fl=S.episodes.filter(e=>e.flag),pd=S.episodes.filter(e=>e.status=="draft"&&e.n<S.next_ep),r=UI.retcon;
 return`<div class="card"><h2>Edit an already-approved episode</h2><p class="mute">Its memory record is replaced, chapter summaries rebuilt, later episodes checked for conflicts, and a continuity rule added for the future.</p>
  <select id="rsel" onchange="loadRetcon()"><option value="">choose an episode…</option>${ap.map(e=>`<option value="${e.n}" ${UI.rn==e.n?"selected":""}>Ep ${e.n}: ${esc(e.title)}</option>`).join("")}</select>
  <div id="rbox" class="${UI.rn?"":"hide"}"><input id="rtitle" type="text"><textarea id="rtext" class="big"></textarea>${BTN("Save & check downstream","post('retcon',{n:UI.rn,title:val('rtitle'),text:$('#rtext').value})","p")}</div></div>
  ${r?`<div class="card"><h2>Retcon result</h2><p>${chip("conflicts: "+(r.conflicts||[]).length,(r.conflicts||[]).length?"bad":"ok")}</p>${table(r.conflicts||[],[["ep","Ep"],["issue","Conflict"]])}<p><b>Continuity rule added:</b> ${esc(r.continuity_note||"–")}</p></div>`:""}
  <div class="card"><h2>Flagged episodes</h2>${table(fl,[["n","Ep"],["title","Title"],["flag","Conflict"],["x","",x=>BTN("Regenerate",`post('regen',{n:${x.n},note:${JSON.stringify(x.flag).replace(/"/g,"&quot;")}})`)]])}</div>
  ${pd.length?`<div class="card"><h2>Redrafted past episodes awaiting approval</h2>${pd.map(e=>`<details><summary>Ep ${e.n}: ${esc(e.title)}</summary><div class="story" id="pd${e.n}">loading…</div>${BTN("Approve",`post('approve_past',{n:${e.n}})`,"g")}</details>`).join("")}</div>`:""}`},

trace(){return`<div class="card"><h2>Trace & cost</h2><div id="tr">loading…</div></div>`}};
const DEADS=["dead","deceased","killed"];
function approvedList(){const a=S.episodes.filter(e=>e.status=="approved").reverse();return`<div class="card"><h2>Approved episodes (${a.length})</h2>${a.map(e=>`<details ontoggle="if(this.open)loadEp(${e.n})"><summary>Ep ${e.n}: ${esc(e.title)} ${e.edited?chip("human-edited","warn"):""} ${e.flag?chip("flagged","bad"):""}</summary><p class="mute">${esc(e.summary)}</p><div class="story" id="ep${e.n}">loading…</div></details>`).join("")||'<p class="mute">none yet</p>'}</div>`}
async function loadEp(n){const e=await get("/api/episode?n="+n);const el=$("#ep"+n);if(el)el.textContent=e.text}
async function loadRetcon(){UI.rn=+val("rsel")||null;if(!UI.rn)return $("#rbox").classList.add("hide");const e=await get("/api/episode?n="+UI.rn);$("#rbox").classList.remove("hide");$("#rtitle").value=e.title;$("#rtext").value=e.text}
async function showBrief(){const b=await get("/api/brief");const el=$("#brief");el.style.display="block";el.textContent=b.brief||"(not available)"}
async function loadTrace(){const t=await get("/api/trace");const el=$("#tr");if(!el)return;
 el.innerHTML=`<div class="metrics">${[["LLM calls",t.totals.calls],["tokens",t.totals.tokens.toLocaleString()],["cost $ (list price)",t.totals.cost]].map(([k,v])=>`<div class="m"><b>${v}</b><span>${k}</span></div>`).join("")}</div>
 <h3>Estimate for ${S.total} episodes</h3><pre class="code">${esc(t.estimate)}</pre>
 <h3>Per step and model</h3>${table(t.agg,[["step","Step"],["model","Model"],["calls","Calls"],["in_tok","In tok"],["out_tok","Out tok"],["cost","Cost $",x=>x.cost.toFixed(4)],["latency","Avg latency s",x=>(x.latency/x.calls).toFixed(1)]])}
 <h3>Decisions, retries, interventions (latest first)</h3>${table(t.events,[["ts","Time",x=>new Date(x.ts*1000).toLocaleTimeString()],["step","Step"],["ep","Ep"],["event","Event",x=>esc(x.event||x.type||(x.passed!==undefined?(x.passed?"critic pass":"critic fail → revise"):""))],["d","Detail",x=>esc(JSON.stringify(Object.fromEntries(Object.entries(x).filter(([k])=>!["ts","step","ep","event"].includes(k)))).slice(0,220))]])}
 <h3>logs/serial.log (tail)</h3><pre class="code">${esc(t.log.join("\n"))}</pre>
 <p>${BTN("Export .md files","post('export')","",0)} <a style="color:var(--acc)" target="_blank" href="/files?run=${RUN}&name=arc_plan.md">arc_plan.md</a> · <a style="color:var(--acc)" target="_blank" href="/files?run=${RUN}&name=story.md">story.md</a> · <a style="color:var(--acc)" target="_blank" href="/files?run=${RUN}&name=hitl_log.md">hitl_log.md</a></p>`}
document.addEventListener("toggle",async e=>{const m=e.target.querySelector?.(".story[id^=pd]");if(e.target.open&&m){const x=await get("/api/episode?n="+m.id.slice(2));m.textContent=x.text}},true);
window.onhashchange=()=>{TAB=location.hash.slice(1)||"plan";render()};
load().then(pollJob);
</script></body></html>"""

if __name__ == "__main__":
    serve()
