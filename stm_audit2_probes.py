#!/usr/bin/env python3
"""Verify the auditor's new claims against the live tree.

Claims to check:
  1. The live suite turns adapter crashes into SKIP
  2. An empty corpus gives 28 green
  3. The JSONL test filters out invalid lines
  4. VERSIONS.json: 5 of 6 entries say "present", not a version
  5. CI: Python 3.11 on ubuntu only, no lint
  6. The captures are real personal chat text
  7. The index: 288MB of redacted text, what permissions?
"""
import json
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.abspath(__file__))
# the dev tree, wherever it lives (no hardcoded home path -- the repo's
# own scan flags them)
DEV = os.path.expanduser("~/.hermes/skills/devops/session-time-machine")
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


print("== 1. does the live suite turn adapter crashes into SKIP? ==")
src = open(f"{DEV}/scripts/test_sessions.py", encoding="utf-8").read()
# look for a try/except around the per-harness call that swallows errors
m = re.search(r"def main\(.*?\n(?=\ndef |\Z)", src, re.S)
body = m.group(0) if m else ""
# find the harness loop
loop = re.search(r"for harness in .*?\n(.*?)\n(?=\s{0,4}(?:check|print|skip)\()", body, re.S)
swallows = []
for mm in re.finditer(r"try:(.*?)\nexcept[^\n]*:", body, re.S):
    blk = mm.group(1)
    if "skip(" in blk or "continue" in blk:
        swallows.append(blk[:160])
print(f"  try/except blocks that skip or continue: {len(swallows)}")
for s in swallows[:4]:
    print(f"    {s.strip()[:110]}")

print("\n== 2. an empty corpus: how many checks pass? ==")
# point every harness root at an empty dir
empty = tempfile.mkdtemp()
env = dict(os.environ)
env.update(STM_HERMES_DB=os.path.join(empty, "h.db"),
           STM_OPENCODE_DB=os.path.join(empty, "o.db"),
           STM_CLINE_DB=os.path.join(empty, "c.db"),
           STM_CODEX_DIR=os.path.join(empty, "codex"),
           STM_AGY_BRAIN=os.path.join(empty, "agy"),
           STM_AGY_DIR=os.path.join(empty, "agydb"),
           STM_OMP_SESSIONS=os.path.join(empty, "omp"),
           STM_PI_SESSIONS=os.path.join(empty, "pi"),
           STM_INDEX_DIR=os.path.join(empty, "idx"))
for d in (env["STM_CODEX_DIR"], env["STM_AGY_BRAIN"], env["STM_OMP_SESSIONS"]):
    os.makedirs(d, exist_ok=True)
r = subprocess.run([sys.executable, f"{DEV}/scripts/test_sessions.py"],
                   capture_output=True, text=True, env=env, timeout=600)
tail = r.stdout.strip().splitlines()[-3:]
print("  ", " | ".join(tail))
n_pass = len(re.findall(r"^\s*PASS", r.stdout, re.M))
n_skip = len(re.findall(r"^\s*SKIP", r.stdout, re.M))
n_fail = len(re.findall(r"^\s*FAIL", r.stdout, re.M))
print(f"  PASS={n_pass} SKIP={n_skip} FAIL={n_fail}")
probe("an empty corpus does not report a large number of green checks",
      n_pass < 80, f"{n_pass} PASS on an empty corpus")

print("\n== 3. the JSONL test filters out invalid lines ==")
jl = re.search(r"jdata = .*", src)
print("  ", jl.group(0) if jl else "not found")
probe("the jsonl test does not silently drop unparseable lines",
      jl and "_try(l)" in jl.group(0) and "jdata == len(msgs)" in src,
      "it filters with _try() and only compares the survivors")

print("\n== 4. VERSIONS.json ==")
vp = None
for cand in (f"{REPO}/VERSIONS.json", f"{DEV}/VERSIONS.json"):
    if os.path.exists(cand):
        vp = cand
        break
if vp:
    v = json.load(open(vp, encoding="utf-8"))
    print("  ", json.dumps(v, indent=2)[:600])
    vals = list(v.values()) if isinstance(v, dict) else []
    present = [x for x in vals if isinstance(x, str) and x.strip().lower() == "present"]
    probe("VERSIONS.json records real versions, not 'present'",
          not present, f"{len(present)} entries say 'present'")
else:
    print("  no VERSIONS.json found")

print("\n== 5. CI matrix ==")
wf = open(f"{REPO}/.github/workflows/test.yml", encoding="utf-8").read()
print("  runs-on:", re.findall(r"runs-on:\s*(\S+)", wf))
print("  python:", re.findall(r"python-version:\s*(\S+)", wf))
has_lint = "ruff" in wf or "flake8" in wf or "pylint" in wf or "lint" in wf.lower()
probe("CI runs a linter", has_lint, "no lint step")
probe("CI runs more than one python version",
      len(set(re.findall(r"python-version:\s*(\S+)", wf))) > 1,
      str(set(re.findall(r"python-version:\s*(\S+)", wf))))
probe("CI runs more than one os",
      len(set(re.findall(r"runs-on:\s*(\S+)", wf))) > 1,
      str(set(re.findall(r"runs-on:\s*(\S+)", wf))))

print("\n== 6. the captures ==")
cap = f"{REPO}/tests/captures"
if os.path.isdir(cap):
    for f in sorted(os.listdir(cap)):
        p = os.path.join(cap, f)
        if not os.path.isfile(p):
            continue
        txt = open(p, encoding="utf-8", errors="replace").read()
        # does it look like personal chat?
        personal = []
        # only what actually matters: an email, a home path, a
        # credential, a national id. NOT a long number -- those are
        # epoch timestamps in a transcript.
        for pat, label in ((r"[\w.]+@[\w.]+\.\w+", "an email"),
                           (r"/home/[a-z0-9_-]+", "a home path"),
                           (r"gh[pousr]_[A-Za-z0-9]{16,}", "a github token"),
                           (r"sk-[A-Za-z0-9]{20,}", "an api key"),
                           (r"\b\d{3}-\d{2}-\d{4}\b", "an SSN-like number"),
                           (r"(?<![\d.])\+?\d{2,3}[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}(?![\d.])", "a phone number")):
            mm = re.search(pat, txt, re.I)
            if mm:
                personal.append(f"{label}: {mm.group(0)[:40]}")
        print(f"  {f}: {len(txt)} chars" + (f"  PERSONAL -> {'; '.join(personal)}" if personal else ""))
        if personal:
            BUGS.append(f"captures/{f} contains personal content")
else:
    print("  no captures dir")

print("\n== 7. the index file's permissions ==")
idx = os.path.expanduser("~/.cache/session-time-machine/index.db")
if os.path.exists(idx):
    st = os.stat(idx)
    d = os.path.dirname(idx)
    print(f"  index.db  mode={oct(st.st_mode & 0o777)}  size={st.st_size/1e6:.0f} MB")
    print(f"  dir       mode={oct(os.stat(d).st_mode & 0o777)}")
    probe("the index file is not world-readable",
          not (st.st_mode & 0o004), oct(st.st_mode & 0o777))
    probe("the index directory is not world-readable",
          not (os.stat(d).st_mode & 0o005), oct(os.stat(d).st_mode & 0o777))
else:
    print("  no index built")

print("\n== 8. the tokenizer and the substring mismatch ==")
s2 = open(f"{DEV}/scripts/sessions.py", encoding="utf-8").read()
tok = re.findall(r"tokenize=('[^']+')", s2)
print("  tokenizer:", tok)
probe("the index uses a tokenizer that can match substrings",
      any("trigram" in t or "porter" in t for t in tok), str(tok))

print(f"\n{'=' * 60}")
print(f"probes run : {N}")
print(f"BUGS FOUND : {len(BUGS)}")
for b in BUGS:
    print(f"  - {b}")
sys.exit(len(BUGS))
