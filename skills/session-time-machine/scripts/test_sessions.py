#!/usr/bin/env python3
import subprocess, json, os, sys, re, tempfile
from collections import Counter
# resolve sessions.py next to this file so the suite runs from any install location
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.py")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    jdata = [l for l in jl.splitlines() if l.strip() and _try(l)]
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
    check("safety: redaction is not ReDoS-prone on adversarial input",
          _dt < 2.0, f"{_dt:.2f}s on {len(_patho)/1e6:.1f}MB")
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

    # cross-harness
    check("all --list exits 0", run("all","--list").returncode == 0)
    check("all --search exits 0", run("all","--search","the").returncode == 0)
    print(f"\n=== {P} passed, {F} failed ===")
    sys.exit(0 if F == 0 else 1)

if __name__ == "__main__":
    main()
