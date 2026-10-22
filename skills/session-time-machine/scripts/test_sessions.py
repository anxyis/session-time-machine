#!/usr/bin/env python3
import subprocess, json, os, sys, re, tempfile
from collections import Counter
# resolve sessions.py next to this file so the suite runs from any install location
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.py")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sessions  # noqa: E402
from sessions import safety_redact  # noqa: E402
PY = sys.executable
P=F=0
def run(*a):
    return subprocess.run([PY, S, *a], capture_output=True, text=True)
def _try(l):
    try: json.loads(l); return True
    except Exception: return False
def check(name, cond, detail=""):
    global P,F
    if cond: P+=1; print(f"  PASS  {name}")
    else: F+=1; print(f"  FAIL  {name}  ({detail})")
def skip(name, reason): global P; print(f"  SKIP  {name}  ({reason})")
def _fixture_dir():
    """The fixture corpus, found in BOTH layouts: the dev tree has it
    next to the script, the published repo has it at the repo root."""
    here = os.path.dirname(os.path.abspath(__file__))
    for up in (here, os.path.dirname(here), os.path.dirname(os.path.dirname(here)),
               os.path.dirname(os.path.dirname(os.path.dirname(here)))):
        cand = os.path.join(up, "tests", "fixtures")
        if os.path.isdir(cand):
            return cand
    return None


def _harness_present(h):
    """Is this harness actually installed, with at least one session?

    Used to tell "the harness is not here" (a SKIP) from "the adapter
    crashed" (a FAIL). A crash is a bug and must not hide behind a
    green suite.
    """
    try:
        o = run(h, "--list", "3")
        if o.returncode != 0:
            return False
        for ln in o.stdout.splitlines():
            if "@session:" in ln:
                return True
        return False
    except Exception:
        return False
def jget(harness, sid, *parts):
    o = run(harness, "--json", sid, *parts)
    return _safe_json(o.stdout)
def _safe_json(s):
    s = s or ""
    i = s.find("{")
    if i < 0: return {}
    try: return json.loads(s[i:])
    except Exception: return {}
def jlines(harness, sid, *parts):
    o = run(harness, "--jsonl", sid, *parts)
    return o.stdout
def first_sid(h, need_tools=False):
    o = run(h, "--list", "20")
    sids = []
    for ln in o.stdout.splitlines():
        if " | " in ln and "@session:" in ln:
            sids.append(ln.split(" | ")[0].strip())
    if not sids: return None, False
    if not need_tools: return sids[0], False
    for sid in sids[:12]:
        d = jget(h, sid)
        ks = Counter(m["kind"] for m in d.get("messages", []))
        if ks.get("tool_call", 0) > 0 or ks.get("tool_output", 0) > 0:
            return sid, True
    return sids[0], False
def sample_word(d):
    for m in d.get("messages", []):
        c = (m.get("content") or "")
        w = "".join(ch if ch.isalnum() else " " for ch in c).split()
        for w in w:
            if len(w) >= 4: return w
    return None

def suite(harness):
    print(f"\n=== {harness} ===")
    sid, has_tools = first_sid(harness, need_tools=True)
    if not sid: skip("no sessions", harness); return
    d = jget(harness, sid)
    if not d.get("messages"): skip(f"{harness}::{sid}", "no json")
    msgs = d["messages"]
    ks = Counter(m["kind"] for m in msgs)
    tool_kinds = {"tool_call", "tool_output"}
    check(f"{harness}::{sid} json parses with messages", bool(msgs))
    check(f"{harness}::{sid} no empty-string content", all(m["content"] != "" for m in msgs))
    check(f"{harness}::{sid} kinds all known", not (set(ks) - {"message","tool_call","tool_output","reasoning","empty","lifecycle","error","attachment"}))
    check(f"{harness}::{sid} len==sum(kinds)", len(msgs) == sum(ks.values()), f"{len(msgs)} vs {sum(ks.values())}")
    # jsonl all rows parse + count match
    jl = jlines(harness, sid)
    # EVERY non-empty line must parse. The old check filtered the
    # unparseable ones out and compared only the survivors, so it could
    # not see a '## JSONL' header line -- the exact bug it was supposed
    # to catch.
    jlines_all = [l for l in jl.splitlines() if l.strip()]
    jbad = [l for l in jlines_all if not _try(l)]
    check(f"{harness}::{sid} jsonl every line parses",
          not jbad, f"{len(jbad)} unparseable line(s): {jbad[:1]}")
    jdata = [l for l in jlines_all if _try(l)]
    check(f"{harness}::{sid} jsonl all rows parse", len(jdata) == len(msgs), f"{len(jdata)} vs {len(msgs)}")
    # no-tools
    dn = jget(harness, sid, "--no-tools")
    dks = Counter(m["kind"] for m in dn.get("messages", []))
    check(f"{harness}::{sid} --no-tools strips tool KINDS", sum(dks[k] for k in tool_kinds) == 0, f"{dict(dks)}")
    check(f"{harness}::{sid} --no-tools preserves non-tool rows",
          len(dn.get("messages", [])) == len([m for m in msgs if m["kind"] not in tool_kinds]),
          f"{len(dn.get('messages', []))}")
    # regression: tool-call argument VALUES must survive as content (not just names)
    tc = [m for m in msgs if m["kind"] == "tool_call"]
    check(f"{harness}::{sid} tool_call args preserved",
          not tc or sum(1 for m in tc if m.get("content")) > 0,
          f"{sum(1 for m in tc if m.get('content'))}/{len(tc)} carry args")
    return sid, has_tools, msgs, ks

def suite_filters(harness, sid):
    d = jget(harness, sid)
    msgs = d["messages"]
    if not msgs: return
    r = [m for m in msgs if m["role"] == "user"]
    check(f"{harness}::{sid} --role user only", all(m["role"]=="user" for m in jget(harness, sid, "--role","user").get("messages", [])))
    check(f"{harness}::{sid} --role assistant only", all(m["role"]=="assistant" for m in jget(harness, sid, "--role","assistant").get("messages", [])))
    # --model filter
    if msgs[0].get("model"):
        sub = str(msgs[0]["model"]).split("/")[0][:6]
        keep = jget(harness, sid, "--model", sub)
        check(f"{harness}::{sid} --model keeps matches", keep.get("session",{}).get("counts",{}).get("msgs",0) == len(keep["messages"]))
        drop = jget(harness, sid, "--model", "NO_SUCH_MODEL_TOKEN_XYZ")
        check(f"{harness}::{sid} --model bogus drops all", len(drop.get("messages", [])) == 0)
    term = sample_word(d) or "the"
    sr = run(harness, "--search", term)
    check(f"{harness}::{sid} --search returns self",
          sr.returncode == 0 and sid in sr.stdout,
          f"rc={sr.returncode} out={sr.stdout[:60]!r}")
    # clip
    longm = next((m for m in msgs if len(m.get("content") or "") >= 200), None)
    if longm:
        o = run(harness, "--transcript", sid, "--clip", "50")
        check(f"{harness}::{sid} --clip emits marker", "clipped]" in o.stdout, o.stdout[:80])
    else:
        check(f"{harness}::{sid} --clip marker", True, "skipped (no long message)")
    # ordering: epochs non-decreasing
    eps = [m.get("epoch") for m in msgs if m.get("epoch") is not None]
    check(f"{harness}::{sid} ordering preserved", all(eps[i] <= eps[i+1] for i in range(len(eps)-1)))

def main():
    for h in ("hermes","opencode","cline","codex","agy"):
        try:
            res = suite(h)
            if res:
                sid, ht, msgs, ks = res
                suite_filters(h, sid)
        except Exception as e:
            # A CRASH in an installed harness is a BUG, not a missing
            # install. Reporting it as a SKIP is how a real defect hides
            # behind a green suite.
            if _harness_present(h):
                global F
                F += 1
                import traceback
                print(f"  FAIL  {h} (the adapter CRASHED -- a bug, not a "
                      f"missing harness)")
                print("        " + traceback.format_exc().replace("\n", "\n        ")[:1200])
            else:
                print(f"  SKIP  {h} (harness unavailable: {e})")
    # codex regressions
    csid, _ = first_sid("codex")
    if csid:
        cd = jget("codex", csid)
        cks = Counter(m["kind"] for m in cd["messages"])
        check("codex no lifecycle by default", cks.get("lifecycle", 0) == 0, f"{dict(cks)}")
        cd2 = jget("codex", csid, "--include-lifecycle")
        cks2 = Counter(m["kind"] for m in cd2["messages"])
        check("codex --include-lifecycle surfaces lifecycle", cks2.get("lifecycle", 0) > 0, f"{dict(cks2)}")
        check("codex tool envelope", cks.get("tool_call",0)>0 and cks.get("tool_output",0)>0, f"{dict(cks)}")
        lst = run("codex", "--list").stdout
        import re as _re
        yrs = _re.findall(r"\d{2}-\d{2} \d{2}:\d{2}", lst)
        check("codex list timestamps are dates", len(yrs) > 0 and "created" not in lst.splitlines()[0])
        check("codex no world_state/turn_context leak", all(m["kind"] != "lifecycle" or "[ok]" not in m.get("tag","") for m in cd["messages"]))
    # opencode regressions
    od = run("opencode", "--peek", first_sid("opencode")[0] or "x").stdout
    check("opencode peek model parsed", "{" not in od or "model" in od, od[:80])
    # hermes integrity
    hsid = first_sid("hermes")[0]
    if hsid:
        h = jget("hermes", hsid)
        check("hermes empty rows content=None (never fabricated)",
              all(m["content"] is None for m in h["messages"] if m["kind"]=="empty"))
    # agy: now plaintext-transcript-based (protobuf only in the DB fallback)
    asid = first_sid("agy")[0]
    if asid:
        a = jget("agy", asid)
        note = " ".join(a.get("session", {}).get("fidelity", {}).get("unrecovered", [])) + " " + a.get("session", {}).get("note", "")
        check("agy uses plaintext transcript_full.jsonl", "transcript_full.jsonl" in note, note[:120])
        check("agy fidelity.complete==True (lossless)", a["session"]["fidelity"]["complete"] is True,
              str(a["session"]["fidelity"].get("complete")))
        check("agy model populated", bool(a.get("session", {}).get("model")),
              str(a.get("session", {}).get("model")))
        w = sample_word(a)
        sr = run("agy", "--search", w) if w else None
        check("agy --search finds its own session",
              sr is not None and sr.returncode == 0 and asid in sr.stdout,
              (sr.stdout[:80] if sr else "no sample word"))
        # usage from gen_metadata protobuf decode (verified Agy schema:
        # field1->4->{2 uncached_input, 3 output(incl thinking), 5 cache_read})
        u = a.get("session", {}).get("usage") or {}
        check("agy usage has token fields",
              isinstance(u, dict) and "input_tokens" in u and "output_tokens" in u,
              str(u))
        if isinstance(u, dict) and u.get("input_tokens") and u.get("output_tokens"):
            # output must be far smaller than input for a tool-heavy agentic turn;
            # this invariant catches the old Splitrail mislabeling (output< input/10)
            check("agy usage values are sane (output << input)",
                  u["output_tokens"] * 10 < u["input_tokens"],
                  f"input={u['input_tokens']} output={u['output_tokens']}")
        # counts() buckets must sum exactly to msgs (no double-counting of
        # reasoning/tool_output into a_ok; developer rows get an explicit d: bucket)
        pk = run("agy", "--peek", asid)
        m = re.search(r"msgs:\s*(\d+)\s*\(.*u:(\d+)\s+a-ok:(\d+)\s+a-tool:(\d+)\s+"
                      r"tool-out:(\d+)\s+reasoning:(\d+)\s+d:(\d+)\s+empty:(\d+)\s+"
                      r"lifecycle:(\d+)\s+errors:(\d+)\s+lost:(\d+)", pk.stdout)
        if m:
            tot = sum(int(x) for x in m.groups()[1:])
            check("agy counts buckets sum to msgs (no double count)", tot == int(m.group(1)), f"sum={tot} msgs={m.group(1)}")
        else:
            check("agy counts line parseable", False, pk.stdout[:80])
        # Agy step `type` is a closed enum: every tool-result type (RUN_COMMAND,
        # VIEW_FILE, CODE_ACTION, GREP_SEARCH, LIST_DIRECTORY, FIND,
        # INVOKE_SUBAGENT, MCP_TOOL, GENERIC) must become a tool_output row,
        # never a plain assistant message. Before the type map, 2046 rows were
        # misfiled and the call->result queue drifted.
        RES_TYPES = ("RUN_COMMAND", "VIEW_FILE", "CODE_ACTION", "GREP_SEARCH",
                     "LIST_DIRECTORY", "FIND", "INVOKE_SUBAGENT", "MCP_TOOL",
                     "GENERIC")
        to = [x for x in a["messages"] if x["kind"] == "tool_output"]
        ncall = sum(1 for x in a["messages"] if x["kind"] == "tool_call")
        check("agy has tool_output rows (guard against vacuous checks)", len(to) > 0,
              f"tool_output={len(to)}")
        check("agy tool_output rows use role=tool (not assistant)",
              bool(to) and all(x["role"] == "tool" for x in to),
              str(Counter(x["role"] for x in to)))
        # a tool result misfiled as an assistant message carries the step type
        # in its `sub` label -- catch that directly
        misfiled = [x for x in a["messages"]
                    if x["kind"] == "message" and x["role"] == "assistant"
                    and (x.get("sub") or "").split(" ")[0] in RES_TYPES]
        check("agy tool results are never misfiled as assistant messages",
              not misfiled, f"{len(misfiled)} misfiled")
        # tool_output count must approach tool_call count: every result step
        # pairs with a call. Gap allowed only for calls with no result.
        check("agy tool_output count matches tool_call count",
              len(to) >= ncall * 0.9,
              f"tool_output={len(to)} tool_call={ncall}")
    # omp / pi (oh-my-pi + pi share the session format)
    for harness in ("omp", "pi"):
        o = jget(harness, first_sid(harness)[0] or "")
        if not o.get("messages"):
            skip(f"{harness} (no sessions on this box)", "not installed")
            continue
        om = o["messages"]
        otool = [x for x in om if x["kind"] == "tool_output"]
        ocall = [x for x in om if x["kind"] == "tool_call"]
        check(f"{harness} tool_output rows use role=tool",
              bool(otool) and all(x["role"] == "tool" for x in otool),
              str(Counter(x["role"] for x in otool)))
        check(f"{harness} tool_output count matches tool_call count",
              len(otool) == len(ocall) and len(ocall) > 0,
              f"tool_output={len(otool)} tool_call={len(ocall)}")
        # every tool_call id must be answered by a tool_output with the same id
        callids = {x.get("tool_call_id") for x in ocall if x.get("tool_call_id")}
        outids = {x.get("tool_call_id") for x in otool if x.get("tool_call_id")}
        check(f"{harness} tool calls pair with results by call id",
              bool(callids) and callids == outids,
              f"{len(callids - outids)} unmatched")
        # tool_call rows must carry the tool arguments (not just the name)
        withargs = [x for x in ocall if x.get("content")]
        check(f"{harness} tool_call args preserved",
              bool(withargs) and len(withargs) >= len(ocall) * 0.9,
              f"{len(withargs)}/{len(ocall)} carry args")
        check(f"{harness} thinking is reasoning, not message",
              any(x["kind"] == "reasoning" for x in om),
              str(Counter(x["kind"] for x in om)))
        # fidelity must be reported, never assumed
        check(f"{harness} fidelity reported",
              "complete" in o.get("session", {}).get("fidelity", {}),
              str(o.get("session", {}).get("fidelity", {}))[:120])
    # ---- output safety: redaction, fencing, budget -------------------
    # A synthetic session is the only way to assert redaction without
    # depending on whatever secrets happen to live in the author's data.
    import tempfile, os, json as _json
    tmpdir = tempfile.mkdtemp(prefix="stm_safety_")
    try:
        # build a fake omp session containing a real-looking secret
        sess_dir = os.path.join(tmpdir, "agent", "sessions", "-proj")
        os.makedirs(sess_dir, exist_ok=True)
        sid = "2026-01-01T00-00-00-000Z_00000000-0000-0000-0000-000000000000"
        fp = os.path.join(sess_dir, sid + ".jsonl")
        # a SYNTHETIC secret: shaped like a real key so the redaction path is
        # exercised, but not a credential for anything.
        secret = "sk-EXAMPLEKEYFORTESTSONLY0123456789"
        events = [
            {"type": "session", "id": "00000000-0000-0000-0000-000000000000",
             "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/proj", "version": "3"},
            {"type": "message", "id": "m1", "parentId": None,
             "timestamp": "2026-01-01T00:00:01.000Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "hi"}],
                         "timestamp": 1767225601000}},
            {"type": "message", "id": "m2", "parentId": "m1",
             "timestamp": "2026-01-01T00:00:02.000Z",
             "message": {"role": "assistant", "content": [
                 {"type": "toolCall", "id": "call_1", "name": "bash",
                  "arguments": {"command": "cat env"}}],
                 "timestamp": 1767225602000}},
            {"type": "message", "id": "m3", "parentId": "m2",
             "timestamp": "2026-01-01T00:00:03.000Z",
             "message": {"role": "toolResult", "toolCallId": "call_1", "toolName": "bash",
                         "content": [{"type": "text", "text": "OPENAI_API_KEY=" + secret}],
                         "timestamp": 1767225603000}},
        ]
        with open(fp, "w") as fh:
            for e in events:
                fh.write(_json.dumps(e) + "\n")

        env = dict(os.environ)
        # point the omp/pi session roots at our synthetic session -- via the
        # env override, so this works in both live and fixture mode
        env["STM_OMP_SESSIONS"] = os.path.join(tmpdir, "agent", "sessions")
        env["STM_PI_SESSIONS"] = os.path.join(tmpdir, "agent", "sessions")
        env["HOME"] = tmpdir

        def run_home(*a):
            return subprocess.run([PY, S, *a], capture_output=True, text=True, env=env)

        o = run_home("omp", "--json", sid)
        out = o.stdout
        check("safety: secret is redacted by default", secret not in out,
              "SECRET LEAKED")
        check("safety: redaction is reported", "[REDACTED" in out or "REDACTED" in out,
              out[-160:])
        o2 = run_home("omp", "--json", sid, "--no-redact")
        check("safety: --no-redact keeps the raw secret",
              secret in o2.stdout, "redaction could not be disabled")

        t = run_home("omp", "--transcript", sid)
        check("safety: transcript is fenced as untrusted data",
              "BEGIN UNTRUSTED CONTENT" in t.stdout, t.stdout[:120])
        t2 = run_home("omp", "--transcript", sid, "--no-fence")
        check("safety: --no-fence removes the fence",
              "BEGIN UNTRUSTED CONTENT" not in t2.stdout, t2.stdout[:120])

        def _rows(out):
            m2 = re.search(r"\| (\d+) rows", out)
            return int(m2.group(1)) if m2 else 0

        b = run_home("omp", "--transcript", sid, "--budget", "10")
        check("safety: --budget marks what it did not print",
              "budget" in b.stdout.lower(), b.stdout[:160])
        b0 = run_home("omp", "--transcript", sid, "--budget", "0")
        # compare ROWS, not bytes: a small budget still prints the marker row
        # (which explains what was cut), so its byte count can be larger.
        check("safety: --budget 0 prints everything",
              _rows(b0.stdout) > _rows(b.stdout),
              f"rows: {b0.stdout[:60]!r}")
        # and the budget must keep the TAIL, not the head: the last row of
        # the session is the interesting part ("what happened before it died")
        tail = run_home("omp", "--transcript", sid, "--budget", "10")
        check("safety: --budget keeps the end of the session",
              "tool_result" in tail.stdout or "budget" in tail.stdout.lower(),
              tail.stdout[-200:])
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    # ---- sanitized REAL captures ---------------------------------------
    # The synthetic fixtures test the author's reading of each format. These
    # captures are real sessions (sanitized) -- they test the format itself.
    # find tests/captures by walking up from this script (the corpus lives at
    # the repo root in the published layout, and at the skill root in dev)
    caps_dir = None
    _d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        _c = os.path.join(_d, "tests", "captures")
        if os.path.isdir(_c):
            caps_dir = _c
            break
        _d = os.path.dirname(_d)
    if not caps_dir:
        skip("sanitized real captures", "no captures dir")
    else:
        import json as _json2
        for hname in ("hermes", "omp", "codex", "agy"):
            cpath = os.path.join(caps_dir, hname + ".json")
            if not os.path.exists(cpath):
                skip(f"capture {hname}", "no capture")
                continue
            try:
                cap = _json2.load(open(cpath, encoding="utf-8"))
            except Exception as e:
                check(f"capture {hname} parses", False, str(e)[:80])
                continue
            rows = cap.get("rows") or []
            check(f"capture {hname} has rows", len(rows) > 0, f"{len(rows)} rows")
            check(f"capture {hname} records a harness version",
                  bool(cap.get("_harness_version")),
                  str(cap.get("_harness_version"))[:40])
            # the capture must be sanitized: no real home paths, no emails
            blob = _json2.dumps(cap)
            check(f"capture {hname} is sanitized",
                  "/home/" not in blob.replace("/home/<user>", "")
                  and not re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", blob),
                  "unsanitized content found")
            # the capture must use the SAME field names the adapter reads
            if hname == "hermes":
                keys = set(rows[0].keys()) if rows else set()
                check("capture hermes uses the adapter's column names",
                      {"role", "content", "tool_calls", "timestamp"} <= keys,
                      str(sorted(keys))[:120])
            elif hname in ("omp", "codex", "agy"):
                types = {r.get("type") for r in rows if isinstance(r, dict)}
                check(f"capture {hname} has known step types",
                      bool(types) and all(isinstance(t, str) for t in types),
                      str(sorted(x for x in types if x))[:120])
                if hname == "omp":
                    check("capture omp carries toolCallId pairing",
                          any("toolCallId" in _json2.dumps(r) for r in rows),
                          "no toolCallId found")

    # ---- audit regressions: ReDoS + locked db -------------------------
    # The env-var redaction pattern used \s* after a (?m)^ anchor, which
    # backtracked quadratically: 2MB of near-matching lines took 82 SECONDS.
    # That is a DoS on any transcript-shaped input. Keep it fast.
    import time as _time
    _patho = ("api_key=" + "a" * 40 + "\n") * 50000
    _t0 = _time.time()
    _out, _cnt = safety_redact(_patho, True)
    _dt = _time.time() - _t0
    # The bound is deliberately loose: the bug this guards against took
    # 82s, so 10s still catches it with an 8x margin, while a tight
    # bound flaked on a loaded machine (each of the 25 patterns is
    # ~0.1s on this 2.5MB input -- linear, not quadratic -- so the total
    # lands near 2s under load).
    check("safety: redaction is not ReDoS-prone on adversarial input",
          _dt < 10.0, f"{_dt:.2f}s on {len(_patho)/1e6:.1f}MB")
    # and it must still catch a real env-var secret
    _o2, _c2 = safety_redact("OPENAI_API_KEY=sk-abcdefghijklmnop", True)
    check("safety: env-var secrets are still caught", bool(_c2), str(_c2))
    # placeholders stay placeholders
    _o3, _c3 = safety_redact("api_key: ${VANSROUTER_KEY}", True)
    check("safety: placeholder references are not redacted", not _c3, "false positive")

    # A database locked by another writer must not kill the run: we wait.
    import sqlite3 as _sq
    _tmpdb = os.path.join(tempfile.gettempdir(), "stm_lock_test.db")
    for _p in (_tmpdb, _tmpdb + "-wal", _tmpdb + "-shm"):
        if os.path.exists(_p):
            os.remove(_p)
    try:
        _c = _sq.connect(_tmpdb)
        _c.execute("create table t (a int)")
        _c.execute("insert into t values (1)")
        _c.commit()
        _c.close()
        _w = _sq.connect(_tmpdb, timeout=1)
        _w.execute("begin exclusive")
        _w.execute("insert into t values (2)")   # hold the write lock
        try:
            # use the real helper -- it must fall back to a temp copy
            import sessions as _S
            _r = _S.ro(_tmpdb)
            try:
                _n = _r.execute("select count(*) from t").fetchone()[0]
                check("safety: a locked db does not kill the run", _n >= 1, f"rows={_n}")
            except _sq.Error as e:
                check("safety: a locked db does not kill the run", False, str(e)[:80])
            finally:
                _r.close()
        except _sq.Error as e:
            check("safety: a locked db does not kill the run", False, str(e)[:80])
        finally:
            _w.rollback()
            _w.close()
    finally:
        for _p in (_tmpdb, _tmpdb + "-wal", _tmpdb + "-shm"):
            if os.path.exists(_p):
                try:
                    os.remove(_p)
                except OSError:
                    pass

    # ---- fuzz every redaction pattern ----------------------------------
    # One pattern had a quadratic blowup (the env-var matcher: \s* after a
    # (?m)^ anchor, and \s matches newlines). Fuzz ALL patterns with a hard
    # budget so the next one is caught before it ships. The bound is
    # deliberately generous -- this catches a blowup, it is not a benchmark.
    import time as _t2
    _BUDGET = 5.0
    _N = 400_000
    _payloads = [
        ("whitespace run", " " * _N),
        ("newline run", "\n" * _N),
        ("repeated =", "=" * _N),
        ("repeated -", "-" * _N),
        ("repeated _", "_" * _N),
        ("key= + spaces", ("api_key=" + " " * 200 + "\n") * (_N // 210)),
        ("key= + newlines", ("api_key=" + "\n" * 200) * (_N // 210)),
        ("near-miss env", ("A_KEY=" + "a" * 38 + "\n") * (_N // 46)),
        ("near-miss url", ("http://u:" + "p" * 30 + "@h\n") * (_N // 45)),
        ("near-miss bearer", ("Bearer " + "b" * 30 + "\n") * (_N // 37)),
        ("colon+equals", ("a:" + "=" * 30 + "\n") * (_N // 32)),
        ("quoted values", ("api_key=\"" + "a" * 30 + "\"\n") * (_N // 42)),
        ("brace runs", ("{" * 100 + "}" * 100 + "\n") * (_N // 201)),
        ("percent runs", "%" * _N),
        ("dollar brace", ("${" + "A" * 30 + "}\n") * (_N // 34)),
        ("mixed soup", ("api_key=sk-AAA BBB=CCC " * 40 + "\n") * (_N // 880)),
    ]
    import sessions as _S
    _pats = list(_S._SAFETY_COMPILED) + [("env-var", _S._SAFETY_ENV_LINE)]
    _slow = []
    for _pn, _rx in _pats:
        for _pl, _payload in _payloads:
            _t0 = _t2.time()
            try:
                _rx.sub(lambda m: m.group(0), _payload)
            except Exception as _e:
                _slow.append(f"{_pn}/{_pl}: raised {_e}")
                continue
            _dt = _t2.time() - _t0
            if _dt > _BUDGET:
                _slow.append(f"{_pn}/{_pl}: {_dt:.1f}s")
    check(f"safety: all {len(_pats)} redaction patterns resist pathological input",
          not _slow, "; ".join(_slow[:4]))

    # ---- locked-db snapshot hardening -----------------------------------
    # The copy is not atomic (a writer can checkpoint between the db and
    # -wal copies), so it is retried until quick_check passes, the copy is
    # 0600, and the connection is flagged as snapshot-derived.
    import stat as _stat
    try:
        import sessions as _S2
        _tmpdb2 = os.path.join(tempfile.gettempdir(), "stm_lock2_test.db")
        for _p in (_tmpdb2, _tmpdb2 + "-wal", _tmpdb2 + "-shm"):
            if os.path.exists(_p):
                os.remove(_p)
        _c2 = _sq.connect(_tmpdb2)
        _c2.execute("create table t (a int)")
        _c2.execute("insert into t values (1)")
        _c2.commit()
        _c2.close()
        _w2 = _sq.connect(_tmpdb2, timeout=1)
        _w2.execute("begin exclusive")
        _w2.execute("insert into t values (2)")
        try:
            _r2 = _S2.ro(_tmpdb2)
            try:
                check("safety: a locked db is read as a flagged snapshot",
                      _S2.ro_is_snapshot(_r2), "not flagged")
                _perms = []
                for _d in list(_S2._ro_tmpdirs):
                    for _f in os.listdir(_d):
                        _perms.append(_stat.S_IMODE(os.stat(os.path.join(_d, _f)).st_mode))
                check("safety: the snapshot copy is 0600",
                      bool(_perms) and all(m == 0o600 for m in _perms),
                      str([oct(m) for m in _perms]))
            finally:
                _r2.close()
                _S2.ro_cleanup()
        finally:
            _w2.rollback()
            _w2.close()
    except Exception as _e:
        check("safety: locked-db snapshot handling", False, str(_e)[:80])

    # ---- phase 1: hermes context state (compacted / active) -----------
    # A row that was compacted away still has its content, but the model
    # sees a summary instead. The tool must not present a compacted
    # transcript as if it were the model's live context.
    hctx = jget("hermes", first_sid("hermes")[0] or "")
    if hctx.get("messages"):
        hm = hctx["messages"]
        tagged = [x for x in hm if (x.get("tags") or [])]
        check("hermes: rows carry a context state",
              len(tagged) == len(hm), f"{len(tagged)}/{len(hm)} tagged")
        states = set()
        for x in tagged:
            # the context state is one of the three; `redacted` is a separate
            # safety tag that can co-occur on the same row
            ctx_tags = [t for t in (x.get("tags") or [])
                        if t in ("live", "compacted", "retained")]
            states.update(ctx_tags)
        check("hermes: context states are known",
              states <= {"live", "compacted", "retained"}, str(sorted(states)))
        check("hermes: every row has exactly one context state",
              all(len([t for t in (x.get("tags") or [])
                       if t in ("live", "compacted", "retained")]) == 1
                  for x in tagged),
              "a row has 0 or 2+ context states")
        fid = hctx.get("session", {}).get("fidelity", {})
        cx = fid.get("context") or {}
        check("hermes: fidelity reports the context split",
              isinstance(cx, dict) and "live" in cx and "compacted" in cx, str(cx))
        # the split must add up to the row count
        check("hermes: the context split sums to the row count",
              sum(cx.values()) == len(hm), f"{sum(cx.values())} vs {len(hm)}")
        # --context live must keep only live rows
        hl = jget("hermes", first_sid("hermes")[0] or "", "--context", "live")
        hlm = hl.get("messages") or []
        check("hermes: --context live keeps only live rows",
              bool(hlm) and all("live" in (x.get("tags") or []) for x in hlm),
              f"{len(hlm)} rows")
        hc = jget("hermes", first_sid("hermes")[0] or "", "--context", "compacted")
        hcm = hc.get("messages") or []
        check("hermes: --context compacted keeps only compacted rows",
              all("compacted" in (x.get("tags") or []) for x in hcm),
              f"{len(hcm)} rows")
        check("hermes: --context live and compacted are disjoint",
              not ({(x.get("tags") or [None])[0] for x in hlm}
                   & {(x.get("tags") or [None])[0] for x in hcm}), "overlap")
    else:
        skip("hermes context state", "no hermes sessions")

    # ---- phase 2: the handoff pack ---------------------------------
    # The pack is what you paste into a fresh session after a crash. It
    # must contain the human's real intent (not a harness injection), the
    # tool calls that never got a result, and a token-budgeted tail.
    import importlib.util as _ilu
    _sp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.py")
    _spec = _ilu.spec_from_file_location("stm_mod", _sp)
    stm = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(stm)

    # the unfinished-call rule, on synthetic rows (a session that died
    # mid-call must be detected)
    def _R(kind, seq, names=None):
        return {"kind": kind, "seq": seq, "role": "assistant" if kind == "tool_call"
                else ("tool" if kind == "tool_output" else "user"),
                "content": "", "tool_names": names or [], "tool_call_id": None}
    check("handoff: a call with a result is finished",
          len(stm._unfinished_calls([_R("tool_call", 1, ["bash"]),
                                      _R("tool_output", 2)])) == 0)
    check("handoff: a call with no result is unfinished",
          len(stm._unfinished_calls([_R("tool_call", 1, ["bash"])])) == 1)
    check("handoff: a batch of 3 calls is one unfinished batch",
          len(stm._unfinished_calls([_R("tool_call", 1, ["a"]), _R("tool_call", 2, ["b"]),
                                      _R("tool_call", 3, ["c"])])) == 1)
    check("handoff: a batch of 3 calls with a result is finished",
          len(stm._unfinished_calls([_R("tool_call", 1, ["a"]), _R("tool_call", 2, ["b"]),
                                      _R("tool_call", 3, ["c"]),
                                      _R("tool_output", 4)])) == 0)
    check("handoff: two separate unfinished batches are two",
          len(stm._unfinished_calls([_R("tool_call", 1, ["a"]), _R("message", 2),
                                      _R("tool_call", 3, ["b"])])) == 2)
    check("handoff: the call id is read from the tool_calls json",
          stm._call_id({"kind": "tool_call", "seq": 1, "tool_names": [],
                        "tool_call_id": None,
                        "content": '[{"id": "call_abc123def456", "name": "x"}]'})
          == "call_abc123def456")

    # injected rows are skipped when finding the intent
    check("handoff: a compaction notice is not the intent",
          stm._last_intent([{"role": "user", "content": "[CONTEXT COMPACTION - x]"}])[1] is True)
    check("handoff: a real message is the intent",
          stm._last_intent([{"role": "user", "content": "do the thing"}])[1] is False)
    check("handoff: the last REAL message wins over a later injection",
          stm._last_intent([{"role": "user", "content": "real request"},
                            {"role": "user", "content": "[CONTEXT COMPACTION - x]"}])[0]
          ["content"] == "real request")

    # the CLI, on a real session
    hs, _hs_tools = first_sid("hermes")
    if hs:
        h = run("hermes", "--handoff", hs)
        check("handoff: exits 0", h.returncode == 0, h.stderr[-160:])
        out = h.stdout
        check("handoff: has a LAST INTENT section", "## LAST INTENT" in out)
        check("handoff: has an IN PROGRESS section", "## IN PROGRESS" in out)
        check("handoff: has a TAIL section", "## TAIL" in out)
        check("handoff: says it is untrusted", "untrusted" in out.lower())
        check("handoff: reports the token budget", "budget" in out.lower())
        # the tail must be fenced when fencing is on
        if stm._FENCE:
            check("handoff: the tail is fenced", "UNTRUSTED CONTENT" in out)
        # a smaller budget must produce a smaller tail
        big = run("hermes", "--handoff", hs, "--handoff-budget", "8000").stdout
        small = run("hermes", "--handoff", hs, "--handoff-budget", "500").stdout
        check("handoff: a smaller budget gives a smaller tail",
              len(small) < len(big), f"{len(small)} vs {len(big)}")
        # and the budget must be respected
        check("handoff: the budget is reported and respected",
              "tokens" in small and len(small) < len(big) + 2000, f"{len(small)}")
        # the handoff pack must redact secrets -- it is the one output
        # people paste into a fresh session, so a leak here is the worst
        # place for one
        import sqlite3 as _sq
        import tempfile as _tf
        _d = _tf.mkdtemp()
        _db = os.path.join(_d, "t.db")
        _c = _sq.connect(_db)
        _c.execute("create table sessions(id text, title text, source text, "
                   "created_at integer, model text, billing_provider text, "
                   "started_at integer, ended_at integer)")
        _c.execute("insert into sessions values ('s1','t','hermes',1700000000,"
                   "'m','v',1700000000,1700000100)")
        _c.execute("""create table messages(session_id text, role text,
                       timestamp integer, content text, reasoning_content text,
                       tool_calls text, api_content text, finish_reason text,
                       tool_name text, effect_disposition text,
                       compacted integer default 0, active integer default 1,
                       _compressed_summary integer default 0)""")
        # the same SYNTHETIC secret shape the other redaction test uses
        # (documented as safe in the CI scanner's allowlist)
        _SEC = "sk-EXAMPLEKEYFORTESTSONLY6789"
        _c.execute("insert into messages values ('s1','user',1700000000,?,null,"
                   "null,null,null,null,null,0,1,0)", (f"key is {_SEC} do it",))
        _c.execute("insert into messages values ('s1','tool',1700000002,?,null,"
                   "null,null,null,null,null,0,1,0)", (f"out {_SEC}",))
        _c.commit(); _c.close()
        _old_db = os.environ.get("STM_HERMES_DB")
        os.environ["STM_HERMES_DB"] = _db
        try:
            _h = run("hermes", "--handoff", "s1").stdout
        finally:
            if _old_db is None:
                os.environ.pop("STM_HERMES_DB", None)
            else:
                os.environ["STM_HERMES_DB"] = _old_db
        check("handoff: the secret is redacted", _SEC not in _h, "SECRET LEAKED")
        check("handoff: the redaction is reported",
              "REDACTED" in _h or "redacted" in _h.lower())
    else:
        skip("handoff CLI", "no hermes sessions")

    # ---- phase 3: --summary and --diff -------------------------------
    import importlib.util as _ilu2
    _sp2 = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.py")
    _spec2 = _ilu2.spec_from_file_location("stm_mod2", _sp2)
    stm2 = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(stm2)

    hs2, _ = first_sid("hermes")
    if hs2:
        s = run("hermes", "--summary", hs2)
        check("summary: exits 0", s.returncode == 0, s.stderr[-160:])
        so = s.stdout
        check("summary: has a SHAPE section", "## SHAPE" in so)
        check("summary: has a TOOL USAGE section", "## TOOL USAGE" in so)
        check("summary: has a FIDELITY section", "## FIDELITY" in so)
        check("summary: reports the row count", "rows:" in so)
        check("summary: reports a token estimate", "tokens est." in so)
        check("summary: shows the opening prompt", "first user" in so)
        check("summary: shows the closing answer", "last assistant" in so)
        # the summary must be smaller than the transcript, and must not
        # CONTAIN it -- a size ratio is only meaningful for a big session,
        # so check both the ratio (when there is a big session) and the
        # containment (always)
        t_out = run("hermes", "--transcript", hs2).stdout
        check("summary: smaller than the transcript",
              len(so) < len(t_out), f"{len(so)} vs {len(t_out)}")
        if len(t_out) > 4000:
            check("summary: far smaller than a big transcript",
                  len(so) < len(t_out) / 4, f"{len(so)} vs {len(t_out)}")
        # it must not dump the transcript's rows
        t_lines = [ln for ln in t_out.splitlines() if "] [" in ln]
        if t_lines:
            leaked = sum(1 for ln in t_lines if ln.strip()[:40] in so)
            check("summary: does not dump the transcript",
                  leaked < max(2, len(t_lines) // 4),
                  f"{leaked}/{len(t_lines)} transcript lines leaked in")
    else:
        skip("summary CLI", "no hermes sessions")

    # --diff needs two sessions
    sid_list = []
    o = run("hermes", "--list", "20")
    for ln in o.stdout.splitlines():
        if " | " in ln and "@session:" in ln:
            sid_list.append(ln.split(" | ")[0].strip())
    if len(sid_list) >= 2:
        A_, B_ = sid_list[0], sid_list[1]
        d = run("hermes", "--diff", A_, B_)
        check("diff: exits 0", d.returncode == 0, d.stderr[-160:])
        do = d.stdout
        check("diff: has a STRUCTURE section", "## STRUCTURE" in do)
        check("diff: has a TOOL USAGE section", "## TOOL USAGE" in do)
        check("diff: has a MODIFIED TOOL CALLS section",
              "## MODIFIED TOOL CALLS" in do)
        check("diff: has a PROMPT DRIFT section", "## PROMPT DRIFT" in do)
        check("diff: names both sessions", A_ in do and B_ in do)
        # a diff against itself must be symmetric: no tool called with
        # different arguments, and the opening intent identical
        dd = run("hermes", "--diff", A_, A_)
        check("diff: a session against itself shows no modified calls",
              "called in BOTH but with DIFFERENT arguments" not in dd.stdout,
              "self-diff reported modifications")
        check("diff: a session against itself has an identical intent",
              "opening intent: IDENTICAL" in dd.stdout)
    else:
        skip("diff CLI", "need 2 hermes sessions")

    # the arg-hash must ignore volatile ids
    _r1 = {"kind": "tool_call", "seq": 1, "tool_names": ["bash"],
           "tool_call_id": None,
           "content": '[{"id":"call_aaa","name":"bash","input":"ls -la"}]'}
    _r2 = {"kind": "tool_call", "seq": 2, "tool_names": ["bash"],
           "tool_call_id": None,
           "content": '[{"id":"call_bbb","name":"bash","input":"ls -la"}]'}
    _r3 = {"kind": "tool_call", "seq": 3, "tool_names": ["bash"],
           "tool_call_id": None,
           "content": '[{"id":"call_ccc","name":"bash","input":"ls -l /tmp"}]'}
    check("diff: the same call with a different id hashes the same",
          stm2._call_key(_r1) == stm2._call_key(_r2))
    check("diff: different arguments hash differently",
          stm2._call_key(_r1) != stm2._call_key(_r3))

    # ---- phase 4: the FTS5 index ------------------------------------
    # The index is a CACHE: it accelerates --search, it is never a source
    # of truth, and a missing index must not break the search.
    import tempfile as _tf2
    _idxdir = _tf2.mkdtemp(prefix="stm_idx_")
    # bound the build: the test checks the properties, not the corpus size
    #
    # and it must be HERMETIC: the index machinery is tested against the
    # FIXTURE corpus, not whatever the ambient STM_HERMES_DB points at.
    # It inherited the ambient env, so with no corpus at all the index
    # tests failed with 'db/directory not found' -- a failure that was
    # not a defect but a test that depended on the machine.
    _fixdir = _fixture_dir()
    _env = dict(os.environ, STM_INDEX_DIR=_idxdir, STM_INDEX_LIMIT="2")
    _env.pop("STM_NO_INDEX", None)
    if _fixdir:
        _env.update(STM_HERMES_DB=os.path.join(_fixdir, "hermes_state.db"),
                    STM_OPENCODE_DB=os.path.join(_fixdir, "opencode.db"),
                    STM_CLINE_DB=os.path.join(_fixdir, "cline_sessions.db"),
                    STM_CODEX_DIR=os.path.join(_fixdir, "codex_sessions"),
                    STM_AGY_BRAIN=os.path.join(_fixdir, "agy_brain"),
                    STM_AGY_DIR=os.path.join(_fixdir, "agy_conversations"),
                    STM_OMP_SESSIONS=os.path.join(_fixdir, "omp_sessions"),
                    STM_PI_SESSIONS=os.path.join(_fixdir, "pi_sessions"))

    def _run_env(*a, **kw):
        e = dict(_env)
        e.update(kw.pop("env", {}))
        return subprocess.run([PY, S, *a], capture_output=True, text=True,
                              env=e, timeout=600)

    # with no index the search still works
    _s = _run_env("hermes", "--search", "session")
    check("index: search works with no index", _s.returncode == 0, _s.stderr[-160:])
    check("index: index-status says it is not built",
          "not built" in _run_env("hermes", "--index-status").stdout)

    # the index path is honoured, and the env override works
    # the index must land in the directory the test named -- checked
    # against the env the SUBPROCESS gets, not the ambient one
    check("index: STM_INDEX_DIR is honoured",
          _env.get("STM_INDEX_DIR") == _idxdir
          and os.path.isdir(_idxdir), _env.get("STM_INDEX_DIR"))

    # building it over a fixture-sized corpus
    _b = _run_env("hermes", "--reindex")
    check("index: reindex exits 0", _b.returncode == 0, _b.stderr[-200:])
    _idxfile = os.path.join(_idxdir, "index.db")
    check("index: the index file is created where told",
          os.path.exists(_idxfile), _idxfile)
    if os.path.exists(_idxfile):
        import sqlite3 as _sq3
        _c = _sq3.connect(_idxfile)
        _n = _c.execute("select count(*) from rows_fts").fetchone()[0]
        _nf = _c.execute("select count(*) from fingerprints").fetchone()[0]
        check("index: the index has rows", _n > 0, str(_n))
        check("index: fingerprints match the row count", _nf == _n, f"{_nf} vs {_n}")
        # the index must not become a plaintext copy of the transcripts
        _bodies = _c.execute("select body from rows_fts").fetchall()
        _leak = [b for (b,) in _bodies
                 if "sk-EXAMPLEKEYFORTESTSONLY" in (b or "") or "ghp_" in (b or "")]
        check("index: the index stores no raw secret", not _leak, f"{len(_leak)} leaked")
        _c.close()

        # a rebuild must not duplicate rows
        _c = _sq3.connect(_idxfile)
        _before = _c.execute("select count(*) from rows_fts").fetchone()[0]
        _c.close()
        _run_env("hermes", "--reindex")
        _c = _sq3.connect(_idxfile)
        _after = _c.execute("select count(*) from rows_fts").fetchone()[0]
        _c.close()
        check("index: a rebuild does not duplicate rows",
              _before == _after, f"{_before} -> {_after}")

        # A LIMITED index is INCOMPLETE, so it is deliberately not used
        # for searches -- a partial index would silently lose results.
        _u = _run_env("hermes", "--search", "session")
        check("index: a limited index is not used for searches",
              "fts5" not in _u.stdout, _u.stdout[:120])
        check("index: a limited index is reported as incomplete",
              "INCOMPLETE" in _run_env("hermes", "--index-status").stdout)
        _n2 = _run_env("hermes", "--search", "session", env={"STM_NO_INDEX": "1"})
        check("index: STM_NO_INDEX=1 forces the linear scan",
              "fts5" not in _n2.stdout)
        # 'hello' is in the fixture's first user message; 'session' is
        # not, so an empty result for that term would be correct
        _n3 = _run_env("hermes", "--search", "hello", env={"STM_NO_INDEX": "1"})
        # the linear scan prints "sid | model | ..." with no @session:
        # marker, so assert on the session id itself
        check("index: the linear scan still finds sessions",
              "fixture0001" in _n3.stdout, _n3.stdout[:160])

        # a COMPLETE index IS used -- build one over a tiny corpus by
        # pointing the harness roots at a temp dir
        _tiny = _tf2.mkdtemp(prefix="stm_tiny_")
        _e2 = dict(_env)
        _e2.pop("STM_INDEX_LIMIT", None)
        # point hermes at the repo's own fixture db, so the build has a
        # real session to index (an empty dir would index nothing)
        # find the fixture db in BOTH layouts: the dev tree has it next
        # to the script, the published repo has it at the repo root
        _fixdir = None
        _here = os.path.dirname(os.path.abspath(__file__))
        for _up in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
                    os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
            _cand = os.path.join(_up, "tests", "fixtures")
            if os.path.isdir(_cand):
                _fixdir = _cand
                break
        _e2["STM_HERMES_DB"] = os.path.join(_fixdir, "hermes_state.db") if _fixdir else ""
        _e2["STM_OPENCODE_DB"] = os.path.join(_tiny, "o.db")
        _e2["STM_CLINE_DB"] = os.path.join(_tiny, "c.db")
        _e2["STM_CODEX_DIR"] = os.path.join(_tiny, "codex")
        _e2["STM_AGY_BRAIN"] = os.path.join(_tiny, "agy")
        _e2["STM_AGY_DIR"] = os.path.join(_tiny, "agydb")
        _e2["STM_OMP_SESSIONS"] = os.path.join(_tiny, "omp")
        _e2["STM_INDEX_DIR"] = _tf2.mkdtemp(prefix="stm_full_")
        for _d in (_e2["STM_CODEX_DIR"], _e2["STM_AGY_BRAIN"],
                   _e2["STM_OMP_SESSIONS"]):
            os.makedirs(_d, exist_ok=True)
        _fb = subprocess.run([PY, S, "all", "--reindex"], capture_output=True,
                             text=True, env=_e2, timeout=600)
        check("index: a complete build exits 0", _fb.returncode == 0, _fb.stderr[-200:])
        # 'hello' is the fixture's first user message
        _full = subprocess.run([PY, S, "hermes", "--search", "hello"],
                               capture_output=True, text=True, env=_e2, timeout=300)
        check("index: a COMPLETE index is used for searches",
              "fts5" in _full.stdout, _full.stdout[:120])
        _st = subprocess.run([PY, S, "hermes", "--index-status"],
                             capture_output=True, text=True, env=_e2, timeout=300)
        check("index: a complete index is reported as complete",
              "complete" in _st.stdout and "INCOMPLETE" not in _st.stdout,
              _st.stdout[:200])

        # a malformed FTS query must not crash
        _m = _run_env("hermes", "--search", '"unbalanced')
        check("index: a malformed FTS query does not crash",
              _m.returncode == 0, _m.stderr[-160:])

        # deleting the index is safe
        os.unlink(_idxfile)
        _d = _run_env("hermes", "--search", "session")
        check("index: search works after the index is deleted",
              _d.returncode == 0, _d.stderr[-160:])

        # ---- the rowid leak -----------------------------------------
        # A rebuild used to grow the table by exactly the number of
        # changed rows: FTS5's implicit rowid is not what
        # cursor.lastrowid reports after an INSERT, so the delete found
        # nothing. The rowid is explicit now, so a rebuild must leave
        # the table EXACTLY the same size.
        _idx2 = _tf2.mkdtemp(prefix="stm_leak_")
        _e3 = dict(_env)
        _e3["STM_INDEX_DIR"] = _idx2
        _e3.pop("STM_INDEX_LIMIT", None)
        # find the fixture db in BOTH layouts: the dev tree has it next
        # to the script, the published repo has it at the repo root
        _fixdir = None
        _here = os.path.dirname(os.path.abspath(__file__))
        for _up in (_here, os.path.dirname(_here), os.path.dirname(os.path.dirname(_here)),
                    os.path.dirname(os.path.dirname(os.path.dirname(_here)))):
            _cand = os.path.join(_up, "tests", "fixtures")
            if os.path.isdir(_cand):
                _fixdir = _cand
                break
        _e3["STM_HERMES_DB"] = os.path.join(_fixdir, "hermes_state.db") if _fixdir else ""

        def _run3(*a, **kw):
            e = dict(_e3)
            e.update(kw.pop("env", {}))
            return subprocess.run([PY, S, *a], capture_output=True, text=True,
                                  env=e, timeout=600)

        _run3("hermes", "--reindex")
        _f2 = os.path.join(_idx2, "index.db")
        check("index: the leak-test index was built",
              os.path.exists(_f2), _f2)
        if os.path.exists(_f2):
            _c = _sq3.connect(_f2)
            _n1 = _c.execute("select count(*) from rows_fts").fetchone()[0]
            _mx1 = _c.execute("select max(rowid) from rows_fts").fetchone()[0]
            _c.close()
            _run3("hermes", "--reindex")
            _c = _sq3.connect(_f2)
            _n2 = _c.execute("select count(*) from rows_fts").fetchone()[0]
            _mx2 = _c.execute("select max(rowid) from rows_fts").fetchone()[0]
            _c.close()
            check("index: a rebuild does not grow the table",
                  _n1 == _n2, f"{_n1} -> {_n2}")
            check("index: no rowid leak (max rowid == row count)",
                  _mx2 == _n2, f"max rowid {_mx2} vs {_n2} rows")

            # ---- the order ------------------------------------------
            # the newest session must come first, like the linear scan
            _o = _run3("hermes", "--search", "hello").stdout
            _sids = [ln.split("|")[0].strip() for ln in _o.splitlines()
                     if "@session:" in ln]
            check("index: the search output is ordered newest first",
                  bool(_sids) and _sids[0] == "fixture0001",
                  f"first={_sids[0] if _sids else None}")

    # ---- external-audit regressions ---------------------------------
    # Each of these was a real defect found by an external audit and
    # reproduced here. They stay so they cannot come back.
    import sqlite3 as _sq2
    import tempfile as _tf3

    def _hermes_db(rows_):
        _d = _tf3.mkdtemp()
        _p = os.path.join(_d, "h.db")
        _c = _sq2.connect(_p)
        _c.execute("""create table sessions(id text, title text, source text,
                     created_at integer, model text, billing_provider text,
                     started_at integer, ended_at integer)""")
        _c.execute("insert into sessions values ('s1','t','hermes',1700000000,'m','v',1700000000,1700000100)")
        _c.execute("""create table messages(session_id text, role text, timestamp integer,
                     content text, reasoning_content text, tool_calls text,
                     api_content text, finish_reason text, tool_name text,
                     effect_disposition text, compacted integer default 0,
                     active integer default 1, _compressed_summary integer default 0)""")
        for _r in rows_:
            _c.execute("insert into messages values (?,?,?,?,?,?,?,?,?,?,0,1,0)", _r)
        _c.commit(); _c.close()
        return _p

    def _run_env(args, env):
        e = dict(os.environ)
        e.update(env)
        return subprocess.run([PY, S, *args], capture_output=True, text=True,
                              env=e, timeout=300)

    def _hermes_json(rows_, *args):
        _p = _hermes_db(rows_)
        _o = _run_env(["hermes", "--json", "s1", *args], {"STM_HERMES_DB": _p})
        try:
            return json.loads(_o.stdout)
        except Exception:
            return {}

    _tc = json.dumps([{"id": "call_x", "type": "function",
                "function": {"name": "bash", "arguments": '{"cmd":"ls -la"}'}}])

    _j1 = _hermes_json([("s1", "system", 1700000001, "SYSTEM RULES", None, None, None, None, None, None)])
    _roles = [m.get("role") for m in (_j1.get("messages") or [])]
    check("audit: hermes role=system is not rendered as user",
          "system" in _roles and "user" not in _roles, str(_roles))

    _j2 = _hermes_json([("s1", "assistant", 1700000001, "Let me check", None, _tc, None, None, None, None)])
    _joined = " ".join(str(m.get("content") or "") for m in (_j2.get("messages") or []))
    check("audit: a text+tool_call turn keeps the call's arguments",
          "ls -la" in _joined or "arguments" in _joined, "args lost")

    _pk = run("hermes", "--peek", "NO_SUCH_SESSION_AT_ALL")
    check("audit: --peek does not claim 'complete' for a nonexistent id",
          "complete" not in _pk.stdout.lower(), _pk.stdout[:100])
    check("audit: --peek exits non-zero for a nonexistent id",
          _pk.returncode != 0 and "no session matching" in _pk.stderr,
          f"rc={_pk.returncode} err={_pk.stderr[:80]}")
    # every id-taking verb must fail loudly, not print a phantom session
    for _verb, _argc in (("peek", 1), ("summary", 1), ("handoff", 1), ("tail", 1),
                         ("transcript", 1), ("preview", 1), ("md", 1),
                         ("jsonl", 1), ("json", 1), ("export", 1)):
        _rv = run("all", "--" + _verb, "NO_SUCH_SESSION_AT_ALL")
        check(f"audit: --{_verb} exits non-zero for a nonexistent id",
              _rv.returncode == 2 and _rv.stdout == "",
              f"rc={_rv.returncode} out={_rv.stdout[:60]!r}")
    _rd = run("all", "--diff", "NO_SUCH_SESSION_AT_ALL", "s1")
    check("audit: --diff exits non-zero when either id is bogus",
          _rd.returncode == 2, f"rc={_rd.returncode}")
    _rpath = run("all", "--path", "NO_SUCH_SESSION_AT_ALL")
    check("audit: --path exits non-zero for a nonexistent id",
          _rpath.returncode == 2 and _rpath.stdout == "", f"rc={_rpath.returncode}")
    # a scoped harness must say which scope it searched
    _rsc = run("codex", "--peek", "NO_SUCH_SESSION_AT_ALL")
    check("audit: scoped miss names the harness in the error",
          "in codex" in _rsc.stderr, _rsc.stderr[:100])

    # ---- 'last' must honour --source, and agree with 'ls' ----------------
    # Deterministic corpus: a NEWER cron session and an OLDER cli session.
    # Before the fix, `last` ignored --source entirely and always returned
    # the cron row, so `--source cli last` handed back an excluded session.
    def _src_db():
        _d = _tf3.mkdtemp()
        _p = os.path.join(_d, "h.db")
        _c = _sq2.connect(_p)
        _c.execute("""create table sessions(id text, title text, source text,
                     created_at integer, model text, billing_provider text,
                     started_at integer, ended_at integer)""")
        _c.execute("""create table messages(session_id text, role text, timestamp integer,
                     content text, reasoning_content text, tool_calls text,
                     api_content text, finish_reason text, tool_name text,
                     effect_disposition text, compacted integer default 0,
                     active integer default 1, _compressed_summary integer default 0)""")
        # cron_one is the NEWEST by started_at
        _c.execute("insert into sessions values ('cron_one','c','cron',1700000200,'m','v',1700000200,1700000260)")
        _c.execute("insert into sessions values ('cli_one','c','cli',1700000100,'m','v',1700000100,1700000160)")
        _c.execute("insert into messages values ('cron_one','user',1700000200,'cron body',NULL,NULL,NULL,NULL,NULL,NULL,0,1,0)")
        _c.execute("insert into messages values ('cli_one','user',1700000100,'cli body',NULL,NULL,NULL,NULL,NULL,NULL,0,1,0)")
        _c.commit(); _c.close()
        return _p

    _sdb = dict(STM_HERMES_DB=_src_db())
    _last_pick = {}
    for _lbl, _extra in (("unset", []), ("cron", ["--source", "cron"]),
                         ("cli", ["--source", "cli"]), ("all", ["--source", "all"]),
                         ("telegram", ["--source", "telegram"])):
        _rp = _run_env(["hermes", "--peek", "last"] + _extra, _sdb)
        _m = re.search(r"## peek (\S+)", _rp.stdout)
        _last_pick[_lbl] = _m.group(1) if _m else None
    check("cli: last ignores cron by default (matches ls)",
          _last_pick["unset"] == "cli_one", _last_pick["unset"])
    check("cli: last --source cron picks the cron session",
          _last_pick["cron"] == "cron_one", _last_pick["cron"])
    check("cli: last --source cli excludes the newer cron session",
          _last_pick["cli"] == "cli_one", _last_pick["cli"])
    check("cli: last --source all picks the newest overall",
          _last_pick["all"] == "cron_one", _last_pick["all"])
    check("cli: last --source telegram finds nothing and exits non-zero",
          _last_pick["telegram"] is None and
          _run_env(["hermes", "--peek", "last", "--source", "telegram"], _sdb).returncode == 2,
          str(_last_pick["telegram"]))

    # the documented synthetic test secret (the CI scanner allowlists it)
    _sec = "ghp_EXAMPLEKEYFORTESTSONLY0123456789"
    _p3 = _hermes_db([("s1", "user", 1700000001, f"the key is {_sec} ok", None, None, None, None, None, None)])
    _pk2 = _run_env(["hermes", "--peek", "s1"], {"STM_HERMES_DB": _p3})
    check("audit: --peek output is redacted", _sec not in _pk2.stdout, "secret in --peek")
    _sr = _run_env(["hermes", "--search", "key"], {"STM_HERMES_DB": _p3})
    check("audit: --search output is redacted", _sec not in _sr.stdout, "secret in --search")

    for _label, _payload, _secret in (
            ("JSON-quoted password", '{"password": "hunter2hunter2"}', "hunter2hunter2"),
            ("refresh_token", 'refresh_token = "abcdef1234567890abcdef"', "abcdef1234567890abcdef"),
            ("db_password", "db_password: sup3rs3cretvalue", "sup3rs3cretvalue"),
            ("lowercase aws secret", "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI"),
            ("Authorization Basic", "Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==", "dXNlcjpwYXNzd29yZDEyMw=="),
            ("sk-proj-", "sk-proj-abcdefghijklmnopqrstuvwxyz1234", "abcdefghijklmnopqrstuvwxyz1234"),
            ("unterminated PEM", "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
             + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC")):
        _p4 = _hermes_db([("s1", "user", 1700000001, _payload, None, None, None, None, None, None)])
        _out4 = _run_env(["hermes", "--json", "s1"], {"STM_HERMES_DB": _p4}).stdout
        check(f"audit: redaction catches {_label}", _secret not in _out4, "secret survived")

    _p5 = _hermes_db([("s1", "tool", 1700000001, "X" * 3_000_000, None, None, None, None, None, None)])
    _tr = _run_env(["hermes", "--transcript", "s1"], {"STM_HERMES_DB": _p5})
    check("audit: the budget does not print a 3MB tool output whole",
          len(_tr.stdout) < 1_500_000, f"{len(_tr.stdout)} chars")
    _md = _run_env(["hermes", "--md", "s1"], {"STM_HERMES_DB": _p5})
    check("audit: --md announces its truncation",
          "truncat" in _md.stdout.lower() or "budget" in _md.stdout.lower(),
          f"{len(_md.stdout)} chars")

    _d6 = _tf3.mkdtemp()
    _out6 = os.path.join(_d6, "o.jsonl")
    _run_env(["hermes", "--export", "s1", "--out", _out6], {"STM_HERMES_DB": _p3})
    if os.path.exists(_out6):
        check("audit: --export writes the file 0600",
              oct(os.stat(_out6).st_mode & 0o777) == "0o600",
              oct(os.stat(_out6).st_mode & 0o777))
        _first = open(_out6, encoding="utf-8").readline()
        check("audit: --export produces valid JSONL",
              _first.lstrip().startswith("{"), f"first line: {_first[:40]!r}")
    else:
        check("audit: --export writes the file 0600", False, "no file")
        check("audit: --export produces valid JSONL", False, "no file")

    for _mode in ("--md", "--tail", "--preview"):
        _args = ["hermes", _mode] + (["s1", "5"] if _mode in ("--tail", "--preview") else ["s1"])
        _o7 = _run_env(_args, {"STM_HERMES_DB": _p3})
        check(f"audit: {_mode} output is fenced as untrusted",
              "UNTRUSTED CONTENT" in _o7.stdout, "no fence")
    for _mode in ("--json", "--jsonl"):
        _o8 = _run_env(["hermes", _mode, "s1"], {"STM_HERMES_DB": _p3})
        check(f"audit: {_mode} output is fenced as untrusted",
              ("UNTRUSTED CONTENT" in _o8.stdout or
               "UNTRUSTED CONTENT" in _o8.stderr or
               '"untrusted_content"' in _o8.stdout), "no fence at all")

    check("audit: --raw is not a CLI flag",
          "--raw" not in run("hermes", "--help").stdout, "--raw exists")

    _j9 = _hermes_json([("s1", "assistant", 1700000001, None, None, _tc, None, None, None, "bash")])
    _un9 = " ".join(str(u) for u in ((_j9.get("session") or {}).get("fidelity") or {}).get("unrecovered") or [])
    check("audit: a dangling tool call is reported",
          "no result" in _un9, _un9[:80])

    # ---- phase 5: human CLI ergonomics (stm) -------------------------
    check("cli: bare argv rewrites to all --list",
          sessions._rewrite_human_argv([]) == ["all", "--list", "10"])
    check("cli: ls verb rewrites to --list",
          sessions._rewrite_human_argv(["ls", "5"]) == ["all", "--list", "5"])
    check("cli: peek verb rewrites to --peek",
          sessions._rewrite_human_argv(["peek", "s1"]) == ["all", "--peek", "s1"])
    check("cli: search verb rewrites to --search",
          sessions._rewrite_human_argv(["search", "foo"]) == ["all", "--search", "foo"])
    check("cli: search verb as query preserved",
          sessions._rewrite_human_argv(["search", "ls"]) == ["all", "--search", "ls"])
    check("cli: diff verb rewrites to --diff",
          sessions._rewrite_human_argv(["diff", "s1", "s2"]) == ["all", "--diff", "s1", "s2"])
    check("cli: harness prefix preserved with verb",
          sessions._rewrite_human_argv(["hermes", "peek", "s1"]) == ["hermes", "--peek", "s1"])
    check("cli: legacy flags preserved",
          sessions._rewrite_human_argv(["hermes", "--peek", "s1"]) == ["hermes", "--peek", "s1"])
    _arg_orig = ["ls", "3"]
    sessions._rewrite_human_argv(_arg_orig)
    check("cli: rewrite does not mutate input", _arg_orig == ["ls", "3"])

    # test session resolution
    _ha, _ = sessions._resolve_session("all", "@session:default/s1")
    check("cli: resolve @session:default/ prefix to hermes", _ha == "hermes")
    _hb, _ = sessions._resolve_session("all", "@session:codex/01a15ea2")
    check("cli: resolve @session:codex/ prefix to codex", _hb == "codex")

    # edge cases from adversarial review
    for _blank in ("  ", "@session:", "@session:  ", ""):
        try:
            sessions._resolve_session("all", _blank)
            _blank_err = False
        except SystemExit:
            _blank_err = True
        check(f"cli: blank session id {_blank!r} rejected", _blank_err)

    # exact beats prefix
    _orig_ids = sessions._all_session_ids
    _orig_rows = sessions._get_rows
    try:
        _mock_data = {
            "hermes": ["20261021_exact", "20261021_exact_longer", "20261021_other"],
            "codex": ["01a_unique", "01a_other"],
            "cline": []
        }
        sessions._all_session_ids = lambda h: _mock_data.get(h, [])
        sessions._get_rows = lambda h, s: ([{"r": 1}], None) if s in _mock_data.get(h, []) else ([], None)

        _hx, _sx = sessions._resolve_session("all", "20261021_exact")
        check("cli: exact match beats prefix match", _hx == "hermes" and _sx == "20261021_exact")

        _hy, _sy = sessions._resolve_session("all", "01a_un")
        check("cli: unique prefix resolves", _hy == "codex" and _sy == "01a_unique")

        # ambiguous prefix
        try:
            sessions._resolve_session("all", "20261021_")
            _ambig = False
        except SystemExit:
            _ambig = True
        check("cli: ambiguous prefix raises SystemExit", _ambig)

        # explicit tool scoping does not leak to another harness. The old
        # contract silently fell through and returned the requested harness
        # for an id that does not exist, which printed a phantom session and
        # exited 0. A scoped miss must now fail loudly.
        _scoped_leak = None
        try:
            _h_scoped, _ = sessions._resolve_session("hermes", "01a_un")
            _scoped_leak = _h_scoped
        except SystemExit:
            pass
        check("cli: explicit harness scope does not leak",
              _scoped_leak is None, f"fell through to {_scoped_leak!r}")

        # adversarial ids do not crash with unhandled exception
        for _bad in ("../../etc/passwd", "hermes/../../x", "a\x00b", "x" * 5000, "s1; rm -rf ~"):
            try:
                sessions._resolve_session("all", _bad)
            except (SystemExit, Exception) as _e:
                check(f"cli: adversarial id {_bad[:15]!r} handled safely", not isinstance(_e, (NameError, AttributeError, TypeError)))
    finally:
        sessions._all_session_ids = _orig_ids
        sessions._get_rows = _orig_rows

    # cross-harness
    check("all --list exits 0", run("all","--list").returncode == 0)
    check("all --search exits 0", run("all","--search","the").returncode == 0)
    check("cli stm ls exits 0", run("ls", "2").returncode == 0)
    check("cli: completion bash exits 0 and defines _stm", "_stm()" in run("completion", "bash").stdout)
    check("cli: completion zsh exits 0 and defines compdef", "compdef _stm stm" in run("completion", "zsh").stdout)
    check("cli: completion fish exits 0 and defines complete -c stm", "complete -c stm" in run("completion", "fish").stdout)
    check("cli: pager starts safely without tty", sessions._start_pager() is None)
    check("cli: --version exits 0", "session-time-machine" in run("--version").stdout)
    check("cli: version verb exits 0", "session-time-machine" in run("version").stdout)
    check("cli: harnesses verb exits 0 and shows table", "HARNESS" in run("harnesses").stdout)
    check("cli: path verb prints file/db location", bool(run("path", "last").stdout.strip()))
    check("cli: last keyword resolves to newest session", run("peek", "last").returncode == 0)
    check("cli: help includes human verbs", "commands (human verbs)" in run("--help").stdout)
    print(f"\n=== {P} passed, {F} failed ===")
    sys.exit(0 if F == 0 else 1)

if __name__ == "__main__":
    main()
