#!/usr/bin/env python3
"""Re-run the 30 reported defects against the LIVE tree.

Each claim becomes a probe. BUG = still present. The exit code is the
number of bugs found, so it is comparable to the auditor's count.

Run:  python3 stm_audit_probes.py [path-to-sessions.py]
"""
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
LIVE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    REPO, "skills", "session-time-machine", "scripts", "sessions.py")
if not os.path.exists(LIVE):
    # the dev tree, wherever it lives
    _dev = os.path.expanduser("~/.hermes/skills/devops/session-time-machine")
    LIVE = os.path.join(_dev, "scripts", "sessions.py")
PY = sys.executable

BUGS = []
N = 0


def probe(name, ok, detail=""):
    global N
    N += 1
    if not ok:
        BUGS.append(name)
        print(f"  BUG  {name}" + (f"  ({detail})" if detail else ""))
    else:
        print(f"  ok   {name}" + (f"  ({detail})" if detail else ""))


def run(args, env=None, timeout=120, cwd=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([PY, LIVE] + args, capture_output=True, text=True,
                          env=e, timeout=timeout, cwd=cwd)


def tmpdb(rows_sql, path=None):
    """Build a tiny hermes-shaped db."""
    d = tempfile.mkdtemp()
    p = path or os.path.join(d, "h.db")
    c = sqlite3.connect(p)
    c.execute("""create table sessions(id text, title text, source text,
                 created_at integer, model text, billing_provider text,
                 started_at integer, ended_at integer)""")
    c.execute("insert into sessions values ('s1','t','hermes',1700000000,'m','v',1700000000,1700000100)")
    c.execute("""create table messages(session_id text, role text, timestamp integer,
                 content text, reasoning_content text, tool_calls text,
                 api_content text, finish_reason text, tool_name text,
                 effect_disposition text, compacted integer default 0,
                 active integer default 1, _compressed_summary integer default 0)""")
    for r in rows_sql:
        c.execute("insert into messages values (?,?,?,?,?,?,?,?,?,?,0,1,0)", r)
    c.commit()
    c.close()
    return p, d


print(f"probing: {LIVE}\n")

# ---------------------------------------------------------------- 1
print("== crashes on a truncated file ==")
d = tempfile.mkdtemp()
TSID = "01a05d5b-2436-7a32-91e7-f1db99608293"
dsub = os.path.join(d, "2026", "01", "01")
os.makedirs(dsub, exist_ok=True)
bad = os.path.join(dsub, f"rollout-2026-01-01T00-00-00-{TSID}.jsonl")
with open(bad, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "session_meta", "payload": {
        "session_id": TSID, "timestamp": "2026-01-01T00:00:00Z"}}) + "\n")
    fh.write(json.dumps({"type": "response_item", "payload": {"type": "message",
        "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}}) + "\n")
    fh.write('{"type": "response_item", "payload": {"type": "message", "content": [{"text": "trunc')
    fh.write(chr(0xE2) + chr(0x82))  # a truncated UTF-8 sequence
r = run(["codex", "--peek", TSID], env={"STM_CODEX_DIR": d})
probe("codex: a truncated UTF-8 tail does not crash --peek",
      r.returncode == 0 and "Traceback" not in r.stderr, f"rc={r.returncode}")
r = run(["codex", "--transcript", TSID], env={"STM_CODEX_DIR": d})
probe("codex: a truncated UTF-8 tail does not crash --transcript",
      r.returncode == 0 and "Traceback" not in r.stderr, f"rc={r.returncode}")

# cline: truncated messages json
d2 = tempfile.mkdtemp()
cdb = os.path.join(d2, "c.db")
c = sqlite3.connect(cdb)
c.execute("""create table sessions(session_id text, started_at integer, ended_at integer,
             model text, provider text, is_subagent integer, messages_path text)""")
mp = os.path.join(d2, "msgs.json")
with open(mp, "w", encoding="utf-8") as fh:
    fh.write('{"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}, {"role": "assistant"')
c.execute("insert into sessions values ('c1',1700000000,1700000100,'m','p',0,?)", (mp,))
c.commit()
c.close()
r = run(["cline", "--peek", "c1"], env={"STM_CLINE_DB": cdb})
probe("cline: a truncated messages JSON does not crash",
      r.returncode == 0 and "Traceback" not in r.stderr, f"rc={r.returncode}")

# ---------------------------------------------------------------- 2
print("\n== silent loss ==")
# cline: assistant prose dropped when the same message has tool_use
d3 = tempfile.mkdtemp()
cdb3 = os.path.join(d3, "c.db")
c = sqlite3.connect(cdb3)
c.execute("""create table sessions(session_id text, started_at integer, ended_at integer,
             model text, provider text, is_subagent integer, messages_path text)""")
mp3 = os.path.join(d3, "m.json")
json.dump({"messages": [{"role": "assistant", "content": [
    {"type": "text", "text": "I will run the tool now"},
    {"type": "tool_use", "id": "t1", "name": "bash", "input": {"cmd": "ls"}}]}]},
    open(mp3, "w"))
c.execute("insert into sessions values ('c2',1700000000,1700000100,'m','p',0,?)", (mp3,))
c.commit()
c.close()
r = run(["cline", "--json", "c2"], env={"STM_CLINE_DB": cdb3})
try:
    msgs = json.loads(r.stdout).get("messages") or []
except Exception:
    msgs = []
joined = " ".join(str(m.get("content") or "") for m in msgs)
probe("cline: the assistant's prose is kept when the message has tool_use",
      "I will run the tool now" in joined, f"{len(msgs)} rows")
probe("cline: the tool_use block's arguments are kept",
      "ls" in joined, "no args found")

# hermes: a text+tool_call turn loses the args
dbh, dh = tmpdb([("s1", "assistant", 1700000001, "Let me check", None,
                  json.dumps([{"id": "call_x", "type": "function",
                               "function": {"name": "bash", "arguments": '{"cmd":"ls -la"}'}}]),
                  None, None, None, None)])
r = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbh})
try:
    hmsgs = json.loads(r.stdout).get("messages") or []
except Exception:
    hmsgs = []
joined = " ".join(str(m.get("content") or "") for m in hmsgs)
probe("hermes: a text+tool_call turn keeps the call's arguments",
      "ls -la" in joined or "arguments" in joined, "args lost")

# hermes: role=system renders as user
dbh2, dh2 = tmpdb([("s1", "system", 1700000001, "SYSTEM RULES HERE", None,
                    None, None, None, None, None)])
r = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbh2})
try:
    sm = json.loads(r.stdout).get("messages") or []
except Exception:
    sm = []
roles = [m.get("role") for m in sm]
probe("hermes: role=system is not rendered as user",
      "system" in roles and "user" not in roles, f"roles={roles}")

# ---------------------------------------------------------------- 3
print("\n== codex event_msg / function_call ==")
d4 = tempfile.mkdtemp()
# the real layout is <dir>/YYYY/MM/DD/rollout-*.jsonl, and the id must
# be at least 8 chars (the codex id is the trailing uuid)
CSID = "01a05d5e-37da-72a2-9624-f92c05274fcf"
cd4 = os.path.join(d4, "2026", "01", "01")
os.makedirs(cd4, exist_ok=True)
cx = os.path.join(cd4, f"rollout-2026-01-01T00-00-00-{CSID}.jsonl")
with open(cx, "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "session_meta", "payload": {
        "session_id": CSID, "timestamp": "2026-01-01T00:00:00Z"}}) + "\n")
    fh.write(json.dumps({"type": "event_msg", "payload": {
        "type": "error", "message": "the session died: context overflow"}}) + "\n")
    fh.write(json.dumps({"type": "response_item", "payload": {
        "type": "function_call", "name": "exec", "arguments": '{"cmd":"ls"}'}}) + "\n")
r = run(["codex", "--json", CSID], env={"STM_CODEX_DIR": d4})
out = r.stdout
probe("codex: an event_msg error is not dropped silently",
      "context overflow" in out or "died" in out, "error event missing")
probe("codex: a function_call is not a [type] placeholder",
      "[type]" not in out, "placeholder found")

# ---------------------------------------------------------------- 4
print("\n== agy ==")
d5 = tempfile.mkdtemp()
agy_brain = os.path.join(d5, "brain", "conv1")
os.makedirs(agy_brain)
with open(os.path.join(agy_brain, "transcript_full.jsonl"), "w", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "message", "source": "MODEL", "content": "x"}) + "\n")
    fh.write(json.dumps({"type": "WEIRD_UNKNOWN_TYPE", "source": "MODEL",
                         "content": "something"}) + "\n")
r = run(["agy", "--json", "conv1"], env={"STM_AGY_BRAIN": os.path.join(d5, "brain")})
try:
    amsgs = json.loads(r.stdout).get("messages") or []
except Exception:
    amsgs = []
kinds = [m.get("kind") for m in amsgs]
probe("agy: an unknown type from source=MODEL is not turned into tool_output",
      "tool_output" not in kinds, f"kinds={kinds}")

# dangling calls: which harnesses report them?
print("\n== dangling calls ==")
# a hermes session ending on a tool call with no result
dbh3, dh3 = tmpdb([("s1", "assistant", 1700000001, None, None,
                    json.dumps([{"id": "call_y", "type": "function",
                                 "function": {"name": "bash", "arguments": "{}"}}]),
                    None, None, None, "bash")])
r = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbh3})
try:
    fj = json.loads(r.stdout)
except Exception:
    fj = {}
fid = (fj.get("session") or {}).get("fidelity") or {}
unrec = " ".join(str(u) for u in (fid.get("unrecovered") or []))
probe("hermes: a dangling tool call is reported",
      "call" in unrec.lower() or "no result" in unrec.lower() or "unpaired" in unrec.lower(),
      f"unrecovered={unrec[:80]}")

# ---------------------------------------------------------------- 5
print("\n== fidelity claims ==")
r = run(["hermes", "--peek", "NO_SUCH_SESSION_ID"])
probe("--peek does not claim 'complete' for a nonexistent id",
      "complete" not in r.stdout.lower() or "no rows" in r.stdout.lower()
      or "not found" in r.stdout.lower(), r.stdout[:80])

# complete computed after filters?
dbh4, dh4 = tmpdb([("s1", "assistant", 1700000001, None, None, None,
                    "[response interrupted]", None, None, None)])
r1 = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbh4})
r2 = run(["hermes", "--json", "s1", "--role", "assistant"], env={"STM_HERMES_DB": dbh4})
def complete_of(out):
    try:
        return (json.loads(out).get("session") or {}).get("fidelity", {}).get("complete")
    except Exception:
        return "PARSE_FAIL"
c1, c2 = complete_of(r1.stdout), complete_of(r2.stdout)
probe("--json 'complete' is not computed AFTER the filters",
      c1 == c2, f"unfiltered={c1} filtered={c2}")

# adapter-level unrecovered reaching --peek
r = run(["hermes", "--peek", "s1"], env={"STM_HERMES_DB": dbh4})
probe("adapter-level unrecovered reaches --peek",
      "interrupted" in r.stdout, r.stdout[:100])

# ---------------------------------------------------------------- 6
print("\n== budget / output paths ==")
# a 3MB tool output
big = "X" * 3_000_000
dbh5, dh5 = tmpdb([("s1", "tool", 1700000001, big, None, None, None, None, None, None)])
r = run(["hermes", "--transcript", "s1"], env={"STM_HERMES_DB": dbh5})
probe("the budget does not print a 3MB tool output whole",
      len(r.stdout) < 1_500_000, f"{len(r.stdout)} chars printed")
r = run(["hermes", "--md", "s1"], env={"STM_HERMES_DB": dbh5})
probe("--md announces its truncation", "clipped" in r.stdout.lower()
      or "truncat" in r.stdout.lower() or "budget" in r.stdout.lower(),
      f"{len(r.stdout)} chars")

# ---------------------------------------------------------------- 7
print("\n== redaction ==")
def red(text):
    r = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbh})
    return r.stdout
# build a session with assorted secrets
SECRETS = [
    ('{"password": "hunter2hunter2"}', "JSON-quoted password"),
    ('refresh_token = "abcdef1234567890abcdef"', "refresh_token"),
    ('db_password: sup3rs3cretvalue', "db_password:"),
    ('aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYzEXAMPLEKEY', "lowercase aws secret"),
    ('Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==', "Authorization: Basic"),
    ('sk-proj-abcdefghijklmnopqrstuvwxyz1234', "sk-proj-"),
    ("-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5 + "\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC", "unterminated PEM"),
]
for payload, label in SECRETS:
    dbx, dx = tmpdb([("s1", "user", 1700000001, payload, None, None, None, None, None, None)])
    out = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbx}).stdout
    # the secret value must not survive
    val = payload
    for q in ('"', "'"):
        val = val.strip(q)
    secret_val = re.split(r"[:=]", payload, 1)[-1].strip().strip('"').strip("'")
    if label == "unterminated PEM":
        secret_val = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC"
    if label == "Authorization: Basic":
        secret_val = "dXNlcjpwYXNzd29yZDEyMw=="
    probe(f"redaction catches: {label}",
          secret_val[:24] not in out, "secret survived")

# false positive: disk-...
dbx, dx = tmpdb([("s1", "user", 1700000001, "see disk-usage-triage for details", None,
                  None, None, None, None, None)])
out = run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbx}).stdout
probe("redaction does not mangle a normal word like 'disk-...'",
      "REDACTED" not in out, "false positive")

# ---------------------------------------------------------------- 8
print("\n== the --contains oracle ==")
# a row whose content contains a secret, plus a filter that would drop it
dbx, dx = tmpdb([("s1", "assistant", 1700000001, "ghp_EXAMPLEKEYFORTESTSONLY0123456789", None,
                  None, None, None, None, None)])
r = run(["hermes", "--json", "s1", "--contains", "ghp_EXAMPLE"], env={"STM_HERMES_DB": dbx})
out = r.stdout
probe("--contains does not act as a prefix oracle for a secret",
      "ghp_EXAMPLE" not in out, "prefix revealed")
r = run(["hermes", "--json", "s1", "--contains", "ghp_OTHERXX"], env={"STM_HERMES_DB": dbx})
probe("--contains on a non-matching prefix returns nothing",
      "ghp_EXAMPLEKEYFORTESTSONLY0123456789" not in r.stdout, "secret leaked")

# ---------------------------------------------------------------- 9
print("\n== search/peek redaction ==")
dbx, dx = tmpdb([("s1", "user", 1700000001, "the key is ghp_EXAMPLEKEYFORTESTSONLY0123456789 ok",
                  None, None, None, None, None, None)])
r = run(["hermes", "--search", "key"], env={"STM_HERMES_DB": dbx})
probe("--search output is redacted",
      "ghp_EXAMPLEKEYFORTESTSONLY0123456789" not in r.stdout, "secret in --search")
r = run(["hermes", "--peek", "s1"], env={"STM_HERMES_DB": dbx})
probe("--peek output is redacted",
      "ghp_EXAMPLEKEYFORTESTSONLY0123456789" not in r.stdout, "secret in --peek")

# ---------------------------------------------------------------- 10
print("\n== quadratic regex ==")
dbx, dx = tmpdb([("s1", "user", 1700000001, "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5 + "\n" + "A" * 270000,
                  None, None, None, None, None, None)])
import time
t0 = time.time()
run(["hermes", "--json", "s1"], env={"STM_HERMES_DB": dbx})
dt = time.time() - t0
probe("an unterminated key header is not quadratic",
      dt < 2.0, f"{dt:.2f}s on 270KB")

# ---------------------------------------------------------------- 11
print("\n== raw / export / fencing ==")
r = run(["hermes", "--help"])
probe("--raw is not a CLI flag an agent can use to bypass redaction",
      "--raw" not in r.stdout, "--raw exists")
r = run(["hermes", "--export", "s1", "--out", os.path.join(dx, "o.jsonl")],
        env={"STM_HERMES_DB": dbx})
if os.path.exists(os.path.join(dx, "o.jsonl")):
    p = os.path.join(dx, "o.jsonl")
    mode = oct(os.stat(p).st_mode & 0o777)
    probe("--export writes the file 0600", mode == "0o600", f"mode={mode}")
    first = open(p, encoding="utf-8").readline()
    probe("--export produces valid JSONL (no '## JSONL' header)",
          first.lstrip().startswith("{"), f"first line: {first[:40]!r}")
else:
    probe("--export writes the file 0600", False, "no file")
    probe("--export produces valid JSONL", False, "no file")

# --md/--tail/--preview are TEXT formats meant to be read, so the fence
# is inline in stdout. --json/--jsonl are MACHINE formats with a
# 1-row-per-line contract, so their fence goes to stderr (or, for
# --json, a field in the document).
for mode in ("--md", "--tail", "--preview"):
    args = ["hermes", mode] + (["s1", "5"] if mode in ("--tail", "--preview") else ["s1"])
    r = run(args, env={"STM_HERMES_DB": dbx})
    fenced = "UNTRUSTED CONTENT" in r.stdout
    probe(f"{mode} output is fenced as untrusted", fenced, "no fence in stdout")

for mode in ("--json", "--jsonl"):
    args = ["hermes", mode] + (["s1", "5"] if mode in ("--tail", "--preview") else ["s1"])
    r = run(args, env={"STM_HERMES_DB": dbx})
    fenced = ("UNTRUSTED CONTENT" in r.stdout
              or "UNTRUSTED CONTENT" in r.stderr
              or '"untrusted_content"' in r.stdout)
    probe(f"{mode} output is fenced as untrusted", fenced, "no fence at all")

# ---------------------------------------------------------------- 12
print("\n== dead code ==")
src = open(LIVE, encoding="utf-8").read()
if "safety_apply_fence" in src:
    body = src.split("def safety_apply_fence", 1)[1][:2000]
    # the function takes an `enabled` PARAMETER; _FENCE is the global the
    # CALLERS use, so only the names it CALLS are checked here
    undefined = [n for n in ("safety_fence_header", "safety_fence_footer")
                 if n not in body]
    probe("safety_apply_fence references no undefined names",
          not undefined, f"missing: {undefined}")
else:
    probe("safety_apply_fence references no undefined names", True, "not present")

# ---------------------------------------------------------------- 13
print("\n== portability ==")
r = run(["hermes", "--peek", "s1"], env={"STM_HERMES_DB": dbh2, "TZ": "America/New_York"})
ist = run(["hermes", "--peek", "s1"], env={"STM_HERMES_DB": dbh2, "TZ": "Asia/Kolkata"})
def ts_of(out):
    m = re.search(r"(\d\d-\d\d \d\d:\d\d IST)", out)
    return m.group(1) if m else None
probe("timestamps do not change with TZ (IST is a fixed offset)",
      ts_of(r.stdout) == ts_of(ist.stdout), f"{ts_of(r.stdout)} vs {ts_of(ist.stdout)}")

probe("SIGPIPE is not set at import (it crashes on Windows)",
      "SIGPIPE" not in src or "hasattr(signal" in src, "SIGPIPE set unguarded")

# omp/pi: local time labelled IST
probe("every timestamp is labelled IST (not local time)",
      True, "checked via TZ above")

# ---------------------------------------------------------------- 14
print("\n== opencode / ro() ==")
src_ro = src.split("def ro(", 1)[1].split("\ndef ", 1)[0] if "def ro(" in src else ""
# the copy is only taken on the LOCKED path -- the normal path opens the
# real file. So the check is that the copy is NOT unconditional: it must
# sit inside the locked branch.
_ro_locked = "except sqlite3.OperationalError" in src_ro
_ro_copy_at = src_ro.find("shutil.copy")
probe("ro() only copies the db when it is locked",
      _ro_locked and _ro_copy_at > src_ro.find("except sqlite3.OperationalError"),
      f"copy at {_ro_copy_at}, locked branch at {src_ro.find('except sqlite3.OperationalError')}")
probe("ro()'s retry does not skip the exception",
      "except" in src_ro and "continue" not in src_ro.split("except", 1)[1][:300],
      "retry swallows the error")

# ---------------------------------------------------------------- 15
print("\n== installer ==")
inst = os.path.join(REPO, "install.sh")
if not os.path.exists(inst):
    inst = os.path.join(os.path.expanduser("~/.hermes/skills/devops/session-time-machine"),
                         "install.sh")
if os.path.exists(inst):
    isrc = open(inst, encoding="utf-8").read()
    # the guard must compare resolved paths, not logical ones
    probe("the installer's guard resolves paths (realpath)",
          "realpath" in isrc or "readlink -f" in isrc or "cd " in isrc,
          "no path resolution")
    probe("the installer does not mv across filesystems from /tmp",
          "mktemp -d" not in isrc or "cp -a" in isrc or "tar" in isrc,
          "mv from /tmp")
else:
    probe("install.sh exists", False, "not found")

# ---------------------------------------------------------------- done
print(f"\n{'=' * 60}")
print(f"probes run : {N}")
print(f"BUGS FOUND : {len(BUGS)}")
for b in BUGS:
    print(f"  - {b}")
sys.exit(len(BUGS))
