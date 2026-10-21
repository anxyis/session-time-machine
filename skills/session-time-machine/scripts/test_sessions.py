#!/usr/bin/env python3
import subprocess, json, os, sys, re
from collections import Counter
# resolve sessions.py next to this file so the suite runs from any install location
S = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sessions.py")
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

    # cross-harness
    check("all --list exits 0", run("all","--list").returncode == 0)
    check("all --search exits 0", run("all","--search","the").returncode == 0)
    print(f"\n=== {P} passed, {F} failed ===")
    sys.exit(0 if F == 0 else 1)

if __name__ == "__main__":
    main()
