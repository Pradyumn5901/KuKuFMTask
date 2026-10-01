"""Thin LLM wrapper: provider routing, retries, JSON parsing, tracing, cost accounting."""
import json, logging, os, re, time, random, traceback
from collections import defaultdict
from pathlib import Path

# ---------- file logging: [file.py] -- date time --- message/exception --- line N ----------
HERE = Path(__file__).resolve().parent
LOG_FILE = Path(os.getenv("LOG_DIR", "logs")) / "serial.log"


class _Fmt(logging.Formatter):
    """For exceptions, report the deepest frame inside this project (where it actually broke),
    not the line that called the logger. The full traceback follows on the next lines."""

    def format(self, r):
        if r.exc_info and r.exc_info[2]:
            ours = [f for f in traceback.extract_tb(r.exc_info[2]) if Path(f.filename).resolve().parent == HERE]
            if ours:
                r.filename, r.lineno = Path(ours[-1].filename).name, ours[-1].lineno
            r.msg = f"{r.msg} | {r.exc_info[0].__name__}: {r.exc_info[1]}"
        return super().format(r)


def get_logger(name="serial"):
    root = logging.getLogger("serial")
    if not root.handlers:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        h = logging.FileHandler(LOG_FILE, encoding="utf-8")
        h.setFormatter(_Fmt("[%(filename)s] -- %(asctime)s --- %(levelname)s %(message)s --- line %(lineno)d",
                            "%Y-%m-%d %H:%M:%S"))
        root.addHandler(h)
        root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        root.propagate = False
    return root if name == "serial" else root.getChild(name)


log = get_logger("llm")

try:  # use the OS trust store (macOS Keychain) so corporate proxies with SSL inspection work
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass


def _why(e):
    """Full cause chain: SDKs often hide the real reason behind 'Connection error.'"""
    parts, c = [f"{type(e).__name__}: {e}"], e.__cause__ or e.__context__
    while c and len(parts) < 5:
        parts.append(f"{type(c).__name__}: {c}")
        c = c.__cause__ or c.__context__
    return " <- ".join(parts)

# USD per 1M tokens (input, output). Verify against your provider's current price sheet.
PRICES = {
    "claude-opus": (5.0, 25.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (1.0, 5.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
    "openai/gpt-oss-20b": (0.075, 0.30),   # Groq list prices (console.groq.com/docs/models)
    "openai/gpt-oss-120b": (0.15, 0.60),
    "qwen/qwen3.8-27b": (0.80, 4.00),
    "mock": (3.0, 15.0),
}
DEFAULT_PROVIDER = os.getenv("LLM_PROVIDER", "groq")  # groq | anthropic | openai | gemini | ollama | mock

# Free / OpenAI-compatible providers. Pick one per model with a prefix: "groq:llama-3.3-70b-versatile".
# Models starting with "gemini" route to Gemini automatically.
PROVIDERS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),  # fully local, no key
}
# Default: log every call at list price even on a free plan, so the per-episode cost cap works and
# `estimate` reports what 200 episodes would really cost. FREE_TIER=1 logs free-provider calls as $0.
FREE = os.getenv("FREE_TIER", "0") == "1"


def route(model):
    """-> (provider, model_name)"""
    head, sep, rest = model.partition(":")
    if sep and head in PROVIDERS:
        return head, rest
    if model.startswith("gemini"):
        return "gemini", model
    if model.startswith("claude"):
        return "anthropic", model
    return DEFAULT_PROVIDER, model


def price(model):
    if FREE and route(model)[0] in PROVIDERS:
        return (0.0, 0.0)
    for k in sorted(PRICES, key=len, reverse=True):
        if model.startswith(k):
            return PRICES[k]
    return (3.0, 15.0)


class PromptTooBig(RuntimeError):
    """Prompt can't fit the provider's per-minute token limit; retrying the same prompt is pointless."""


def parse_json(text):
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = min([i for i in (t.find("{"), t.find("[")) if i >= 0], default=-1)
    end = max(t.rfind("}"), t.rfind("]"))
    if start < 0 or end < start:
        raise ValueError("no JSON found")
    return json.loads(t[start:end + 1])


class LLM:
    def __init__(self, run_dir: Path):
        self.trace = run_dir / "trace.jsonl"
        self.forced = os.getenv("LLM_PROVIDER")  # "mock" short-circuits every call
        self._spent = defaultdict(float)
        if self.trace.exists():
            for line in self.trace.read_text().splitlines():
                r = json.loads(line)
                if "cost" in r:
                    self._spent[r.get("ep")] += r["cost"]

    # ---------- observability ----------
    def log(self, exc=None, **kw):
        """Structured trace (trace.jsonl) + human-readable line in logs/serial.log at the caller's file/line."""
        with self.trace.open("a") as f:
            f.write(json.dumps({"ts": round(time.time(), 2), **kw}, ensure_ascii=False) + "\n")
        warn = exc is not None or kw.get("event") in ("api_error", "rate_limited", "too_large", "json_retry", "stop_revising")
        msg = " ".join(f"{k}={v}" for k, v in kw.items() if k != "error")
        log.log(logging.WARNING if warn else logging.INFO, msg, exc_info=exc, stacklevel=2)

    def spent(self, ep="__all__"):
        return sum(self._spent.values()) if ep == "__all__" else self._spent.get(ep, 0.0)

    # ---------- providers ----------
    def _throttle(self):  # free tiers cap requests/minute: space calls out
        rpm = float(os.getenv("LLM_RPM", "0"))
        if rpm:
            wait = getattr(self, "_last", 0) + 60 / rpm - time.time()
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()

    def _raw(self, step, model, system, user, max_tokens, json_out=False):
        p, model = ("mock", model) if self.forced == "mock" else route(model)
        self._throttle()
        if p == "mock":
            out = _mock(step, user)
            return out, len(system + user) // 4, len(out) // 4
        if p == "groq":  # native Groq SDK; reads GROQ_API_KEY from the environment
            from groq import Groq
            if not os.getenv("GROQ_API_KEY"):
                raise SystemExit("Set GROQ_API_KEY (free key: https://console.groq.com/keys).")
            self._g = getattr(self, "_g", None) or Groq()
            gptoss = model.startswith("openai/gpt-oss")
            qwen_effort = os.getenv("QWEN_REASONING", "default") if model.startswith("qwen/") else None
            thinks = gptoss or qwen_effort in ("low", "medium", "high")
            # Free tier counts input + max_completion_tokens against TPM (8K): size the output to fit.
            tpm = int(os.getenv("GROQ_TPM", "8000"))
            est_in = int((len(system) + len(user)) / 3.6) + 100    # English ~4 chars/token; stay a bit conservative
            room = tpm - est_in - 300
            if room < 600:
                raise PromptTooBig(f"[{step}] prompt is ~{est_in} tokens: too big for Groq's {tpm} TPM limit "
                                   f"(Developer plan: raise GROQ_TPM).")
            want = max_tokens + (2048 if thinks else 0)             # reasoning tokens count as output
            out = max(512, int(min(want, room, int(os.getenv("LLM_MAX_OUT", "32000"))) * self._shrink))
            # JSON mode only for gpt-oss: Qwen (preview) returned error objects instead of content in JSON mode.
            kw = {"response_format": {"type": "json_object"}} if json_out and gptoss else {}
            if gptoss:
                kw["reasoning_effort"] = (os.getenv("GROQ_REASONING", "medium") if step in ("write", "revise")
                                          else os.getenv("GROQ_REASONING_CHEAP", "low"))
            elif qwen_effort:  # Qwen: default = no reasoning tokens; low/medium/high = think (hidden from content)
                kw["reasoning_effort"] = qwen_effort
                if qwen_effort not in ("none", "default"):
                    kw["reasoning_format"] = "hidden"
            r = self._g.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=1, top_p=1, stream=False, stop=None, max_completion_tokens=out, **kw)
            text = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S).strip()
            if not text:
                raise RuntimeError("empty completion (budget used up by reasoning?)")
            return text, r.usage.prompt_tokens, r.usage.completion_tokens
        if p == "anthropic":
            import anthropic
            self._a = getattr(self, "_a", None) or anthropic.Anthropic()
            r = self._a.messages.create(model=model, max_tokens=max_tokens, system=system,
                                        messages=[{"role": "user", "content": user}])
            return "".join(b.text for b in r.content if b.type == "text"), r.usage.input_tokens, r.usage.output_tokens
        from openai import OpenAI
        self._clients = getattr(self, "_clients", {})
        if p not in self._clients:
            if p in PROVIDERS:
                url, env = PROVIDERS[p]
                key = os.getenv(env) if env else "ollama"
                if not key:
                    raise SystemExit(f"Set {env} to use {p} (free key; see README).")
                self._clients[p] = OpenAI(base_url=os.getenv(f"{p.upper()}_BASE_URL", url), api_key=key)
            else:
                self._clients[p] = OpenAI(base_url=os.getenv("OPENAI_BASE_URL") or None)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        if p in PROVIDERS:  # thinking models spend output tokens on reasoning: leave headroom
            r = self._clients[p].chat.completions.create(model=model, max_tokens=min(max_tokens * 4, int(os.getenv("LLM_MAX_OUT", "32000"))), messages=msgs)
        else:
            r = self._clients[p].chat.completions.create(model=model, max_completion_tokens=max_tokens, messages=msgs)
        text = r.choices[0].message.content or ""
        if not text.strip():
            raise RuntimeError("empty completion (output budget used up by reasoning?)")
        u = r.usage
        return text, getattr(u, "prompt_tokens", 0) or 0, getattr(u, "completion_tokens", 0) or 0

    def call(self, step, system, user, model, max_tokens=2000, json_out=False, ep=None, need=None, fallback=None):
        """need: keys the JSON must contain (a valid-JSON error object is still a failure).
        fallback: model to try once if this one keeps returning bad output."""
        self._broken = getattr(self, "_broken", set())
        if fallback and model in self._broken:  # this model already failed this session: skip straight to fallback
            model, fallback = fallback, None
        tries = int(os.getenv("LLM_RETRIES", "6"))
        bad_outputs = 0
        self._shrink = 1.0  # reduced after a 413 (request too large)
        for attempt in range(tries):
            t0 = time.time()
            try:
                text, tin, tout = self._raw(step, model, system, user, max_tokens, json_out)
            except (SystemExit, PromptTooBig) as e:
                if isinstance(e, PromptTooBig):
                    self.log(step=step, ep=ep, event="prompt_too_big", error=str(e))
                raise
            except ImportError as e:
                log.error("missing package", exc_info=e)
                raise SystemExit(f"Missing package: {e}. Run: uv sync  (Claude: uv sync --extra claude; Gemini/OpenRouter/Ollama: uv sync --extra openai)")
            except Exception as e:  # network / rate limit
                why = _why(e)
                if "401" in why or "invalid api key" in why.lower():
                    log.error("step=%s authentication failed", step, exc_info=e)
                    raise SystemExit(f"Authentication failed: check your API key. ({why[:200]})")
                if "CERTIFICATE_VERIFY_FAILED" in why or "certificate" in why.lower():
                    log.error("step=%s SSL certificate rejected: %s", step, why[:400], exc_info=e)
                    raise SystemExit(f"SSL certificate rejected (corporate proxy?). Run via `uv run main.py` so truststore is "
                                     f"installed, or set SSL_CERT_FILE=/path/to/corp-ca.pem.\n{why[:300]}")
                low = why.lower()
                too_big = "413" in why or "request too large" in low
                limited = "429" in why or "rate limit" in low or "quota" in low
                m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s", low)
                if too_big:
                    self._shrink *= 0.6          # retrying the identical request can never succeed
                    wait = 2
                elif m:                          # the provider tells us exactly how long to wait
                    wait = int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3)) + 1
                else:
                    wait = min(90, 20 * (attempt + 1)) if limited else 2 ** attempt * 3
                event = "too_large" if too_big else "rate_limited" if limited else "api_error"
                self.log(exc=e, step=step, ep=ep, event=event, attempt=attempt, wait=round(wait, 1), error=why[:600])
                if "per day" in low and wait > 900:
                    raise SystemExit(f"Daily token quota for {model} exhausted (resets in ~{wait / 60:.0f} min). "
                                     f"Progress is saved: rerun the same command later, or switch that step's model.")
                print(f"  [{step}] {event}: {why[:200]}\n  retrying in {wait:.0f}s ({attempt + 1}/{tries})")
                time.sleep(wait)
                continue
            pi, po = price(model)
            cost = (tin * pi + tout * po) / 1e6
            self._spent[ep] += cost
            self.log(step=step, ep=ep, model=model, in_tok=tin, out_tok=tout, cost=round(cost, 5),
                     latency=round(time.time() - t0, 2), attempt=attempt)
            if not json_out:
                return text
            try:
                out = parse_json(text)
                missing = [k for k in (need or []) if not isinstance(out, dict) or k not in out]
                if missing:
                    raise ValueError(f"missing keys {missing}; got {text[:200]!r}")
                return out
            except (ValueError, json.JSONDecodeError) as e:
                bad_outputs += 1
                self.log(step=step, ep=ep, event="json_retry", attempt=attempt, model=model, error=str(e)[:300])
                print(f"  [{step}] bad output from {model} ({str(e)[:120]}); retrying")
                if bad_outputs >= 3:
                    break
                user += ("\n\nIMPORTANT: respond with ONLY the requested JSON object"
                         + (f" containing the keys {need}" if need else "") + ". No prose, no code fences.")
        if fallback and fallback != model:
            self.log(step=step, ep=ep, event="fallback_model", model=model, fallback=fallback)
            print(f"  [{step}] {model} keeps failing; using {fallback} for the rest of this session")
            self._broken.add(model)
            return self.call(step, system, user, fallback, max_tokens, json_out, ep, need, None)
        log.error("step=%s ep=%s gave up after %d attempts", step, ep, tries)
        raise RuntimeError(f"LLM step '{step}' failed after {tries} attempts (see {LOG_FILE})")


# ---------- offline mock so the full pipeline can be exercised without a key ----------
_WORDS = ("rain scooter stairwell door number ledger ghost tenant mailbox receipt fourth floor "
          "lift static signature address parcel smell of smoke Asha knocked waited nobody answered").split()


def _mock(step, user):
    if step in ("plan_arc", "plan_revise"):
        acts = [{"n": i + 1, "title": f"Act {i + 1}", "start": i * 25 + 1, "end": i * 25 + 25,
                 "summary": f"Mock act {i + 1}", "turning_point": f"Turn {i + 1}"} for i in range(8)]
        return json.dumps({"title": "Last Mile", "logline": "Mock", "tone": "noir", "pov": "close third, past",
                           "setting": "Mumbai, Building 14", "world_rules": ["The dead cannot touch objects"],
                           "characters": [{"name": "Asha Rao", "role": "protagonist", "desc": "rider", "arc": "a->b", "secret": "s"},
                                          {"name": "Vikram Shah", "role": "antagonist", "desc": "dispatcher", "arc": "a->b", "secret": "s"}],
                           "acts": acts, "threads": [{"id": "T1", "desc": "Who sends the route?", "opens_by": 1, "resolves_by": 190}],
                           "ending": "Mock ending"})
    if step == "expand_beats":
        a, b = map(int, re.search(r"episodes (\d+)-(\d+)", user).groups())
        return json.dumps({"beats": [{"ep": e, "beat": f"Mock beat {e}", "hook": "hook", "chars": ["Asha Rao"],
                                      "threads": ["T1"]} for e in range(a, b + 1)]})
    if step in ("write", "revise"):
        return "TITLE: Mock Episode\n\n" + " ".join(random.choice(_WORDS) for _ in range(520)) + "\n\nThe door opened."
    if step == "critic":
        return json.dumps({"hook": 8, "momentum": 7, "prose": 7, "beat_followed": True, "consistency_issues": [],
                           "repetition_issues": [], "directive_violations": [], "fix_notes": ""})
    if step == "extract":
        n = re.search(r"EPISODE (\d+)", user).group(1)
        return json.dumps({"summary": f"Mock summary {n}", "hook": "door opened", "signature": f"move {n}",
                           "timeline": f"Day {n}", "facts": [f"fact from ep {n}"],
                           "characters": [{"name": "Asha Rao", "status": "alive", "change": "shaken"}],
                           "relationships": [], "threads_opened": [], "threads_advanced": ["T1"], "threads_closed": []})
    if step == "feedback":
        return json.dumps({"directive": "Mock directive", "revised_beats": [], "rationale": "mock"})
    if step == "retcon_impact":
        return json.dumps({"conflicts": [], "continuity_note": "Mock continuity note"})
    return "Mock chapter summary."
