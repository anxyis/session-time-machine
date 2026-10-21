#!/usr/bin/env python3
"""session-time-machine: a complete, deterministic AI conversation inspector.

Reads conversation history from every supported harness and renders it in ONE
normalized, tag-annotated row format so any model (27B..frontier) can read it
with zero schema reasoning and zero hallucination. Hard rules:

  * NEVER fabricate. A row with no recoverable content is tagged
    [empty: content lost - DO NOT FABRICATE] and content=null in JSON.
  * NEVER silently truncate/drop/reorder. Anything not rendered is counted and
    listed under 'unrecovered'. --clip N truncates only when asked, and marks it.
  * Filtering SELECTS rows; it never mutates a row's meaning.
  * All timestamps are converted to IST here. Hermes=epoch s, OpenCode=epoch ms.
  * Tool-call steps and reasoning are tagged, never rendered as a reply.

Harnesses: hermes, opencode, cline, codex, agy. Every harness feeds the SAME
row shape, so filtering/search/export/printing are shared, not re-implemented.

Usage:
  sessions.py <harness|all> --list [N] [--since DAYS] [--until DAYS] [--source X]
  sessions.py <harness> --peek ID | --transcript ID [N] | --tail ID [N] | --preview ID
  sessions.py <harness> --md ID | --jsonl ID | --json ID | --search TERM
  sessions.py <harness> --export ID --out FILE [--format md|jsonl|json|txt]
Filters (composable, AND-ed; apply to any row-producing command):
  --role R[,R]        user,assistant,tool,system,developer,meta
  --kind K[,K]        message,tool_call,tool_output,reasoning,attachment,lifecycle,empty,error
  --model SUBSTR      match model name      --provider SUBSTR  match provider
  --since DAYS        rows newer than N days ago
  --until DAYS        rows older than N days ago
  --date YYYY-MM-DD   rows on that date (IST)
  --contains TEXT     case-insensitive content match
  --tool NAME         rows invoking/using that tool
  --errors            only rows flagged as errors/interruptions
  --no-tools          drop tool_call+tool_output rows (token saver)
  --include-lifecycle show internal lifecycle rows (hidden by default)
  --clip N            truncate display body to N chars with an explicit marker
"""
import argparse, collections, json, sqlite3, sys, os, glob, datetime, signal, re
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # clean exit when piped to head

HB = os.path.expanduser("~/.hermes/state.db")
ODB = os.path.expanduser("~/.local/share/opencode/opencode.db")
CLINEDB = os.path.expanduser("~/.cline/data/db/sessions.db")
CLINE_SESSION_DIR = os.path.expanduser("~/.cline/data/sessions")
CODEX_SESSION_DIR = os.path.expanduser("~/.codex/sessions")
AGY_DIR = os.path.expanduser("~/.gemini/antigravity-cli/conversations")

HARNESSES = ["hermes", "opencode", "cline", "codex", "agy"]

def ro(path):
    """Open a sqlite DB strictly read-only via URI. Raises FileNotFoundError if absent."""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)

# ---------- time helpers (epoch s / epoch ms / ISO -> IST) ----------

_IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

def ist(epoch_s):
    """epoch seconds (int/float) -> 'MM-DD HH:MM IST'."""
    return (datetime.datetime.fromtimestamp(float(epoch_s), _IST)).strftime("%m-%d %H:%M IST")

def to_epoch(v):
    """Best-effort: value (epoch s / epoch ms / ISO str) -> epoch seconds (float) or None."""
    try:
        if v is None: return None
        if isinstance(v, str):
            s = v.strip()
            if not s: return None
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.timestamp()
        if isinstance(v, (int, float)):
            if v > 1e12: v = v / 1000.0   # ms -> s
            return float(v)
    except Exception:
        return None
    return None

def ist_any(v):
    e = to_epoch(v)
    return ist(e) if e is not None else "?"

def ist_day(e):
    """epoch s -> 'YYYY-MM-DD' in IST (for --date filtering)."""
    return datetime.datetime.fromtimestamp(e, _IST).strftime("%Y-%m-%d") if e is not None else None

def clip(s, n=90):
    return " ".join(str(s or "").split())[:n]

def _join(parts):
    return " ".join(p for p in parts if p)
# ---------- normalized row: one shape for every harness ----------
# Keys are stable; printers/filters/search/export depend only on this shape.

KINDS = {"message", "tool_call", "tool_output", "reasoning", "attachment",
         "lifecycle", "empty", "error"}

def row(harness, sid, seq, role, ts, kind, content=None, reasoning=None,
        tool_names=None, tool_call_id=None, model=None, provider=None,
        is_error=False, attachments=None, unrecovered=None, sub="", display=None,
        epoch=None, tags=None):
    """Build one normalized row. content is verbatim text or None (never invented)."""
    content = content if content else None
    reasoning = reasoning if reasoning else None
    if tags is None:
        tags = []
    r = {
        "harness": harness, "sid": sid, "seq": seq, "role": role,
        "epoch": epoch, "ts": ts, "kind": kind,
        "content": content, "reasoning": reasoning,
        "tool_names": tool_names or [], "tool_call_id": tool_call_id,
        "model": model, "provider": provider, "is_error": bool(is_error),
        "attachments": attachments or [], "unrecovered": unrecovered or [],
        "sub": sub, "_display": display, "tags": tags,
    }
    return r

# tag text per kind (display only; filtering uses r['kind'])
def kind_tag(r):
    k = r["kind"]
    if k == "tool_call":    return "[tool-call-step]"
    if k == "tool_output":  return "[tool-output]"
    if k == "reasoning":    return "[thinking: CoT, NOT the reply]"
    if k == "empty":        return "[empty: content lost - DO NOT FABRICATE]"
    if k == "lifecycle":    return "[lifecycle]"
    if k == "error":        return "[error]"
    if r["role"] == "assistant": return "[ok] assistant-reply"
    return "[ok]"

def body_of(r):
    """The display body: verbatim content, else reasoning placeholder, else ''."""
    if r["_display"] is not None:
        return r["_display"]
    if r["content"]:
        return r["content"]
    if r["kind"] == "reasoning" and r["reasoning"]:
        return f"(chain-of-thought only, {len(r['reasoning'])} chars; not a reply)"
    return ""

def sub_of(r):
    bits = []
    if r["tool_names"]:
        bits.append("tool_call: " + ", ".join(r["tool_names"]))
    if r["reasoning"] and r["kind"] != "reasoning":
        bits.append(f"[thinking: {len(r['reasoning'])} chars CoT]")
    if r["tool_call_id"]:
        bits.append(f"callID={r['tool_call_id']}")
    if r["is_error"]:
        bits.append("[ERROR]")
    bits.extend(r["unrecovered"])
    if r["sub"]: bits.append(r["sub"])
    return " | ".join(bits)

# ---------- fidelity / completeness accounting ----------
def fidelity_report(rows, sid=None, harness=None):
    """Counts + explicit 'what was not recovered' so nothing is silently lost."""
    c = {k: 0 for k in KINDS}
    words = 0
    unrec = []
    for r in rows:
        c[r["kind"]] = c.get(r["kind"], 0) + 1
        words += len(r["content"] or "")
        for u in r["unrecovered"]:
            unrec.append(f"seq{r['seq']}: {u}")
    return {
        "harness": harness, "id": sid, "rows": len(rows), "by_kind": c,
        "content_chars": words,
        "unrecovered": unrec,
        "complete": len(unrec) == 0,
    }
# ---------- composable filter engine ----------
# Filtering SELECTS rows (AND semantics). It never mutates row content, so the
# meaning of any surviving row is identical with or without filters.

class Filt:
    def __init__(self, a):
        self.roles = _csv(getattr(a, "role", None))
        self.kinds = _csv(getattr(a, "kind", None))
        self.model = getattr(a, "model", None)
        self.provider = getattr(a, "provider", None)
        self.since = getattr(a, "since", None)
        self.until = getattr(a, "until", None)
        self.date = getattr(a, "date", None)
        self.contains = getattr(a, "contains", None)
        self.tool = getattr(a, "tool", None)
        self.errors_only = bool(getattr(a, "errors", False))
        self.no_tools = bool(getattr(a, "no_tools", False))
        self.include_lifecycle = bool(getattr(a, "include_lifecycle", False))

    def apply(self, rows):
        now = datetime.datetime.now(datetime.timezone.utc).timestamp()
        out = []
        for r in rows:
            # lifecycle rows are internal; hidden unless explicitly requested
            if r["kind"] == "lifecycle" and not self.include_lifecycle:
                continue
            if self.no_tools and r["kind"] in ("tool_call", "tool_output"):
                continue
            if self.roles and r["role"] not in self.roles:
                continue
            if self.kinds and r["kind"] not in self.kinds:
                continue
            if self.errors_only and not r["is_error"] and r["kind"] != "error":
                continue
            if self.model and self.model.lower() not in (r["model"] or "").lower():
                continue
            if self.provider and self.provider.lower() not in (r["provider"] or "").lower():
                continue
            if self.tool:
                t = self.tool.lower()
                if not any(t in n.lower() for n in r["tool_names"]):
                    continue
            if self.contains:
                hay = ((r["content"] or "") + " " + (r["reasoning"] or "")).lower()
                if self.contains.lower() not in hay:
                    continue
            if self.date:
                if ist_day(r["epoch"]) != self.date:
                    continue
            if self.since is not None:
                if r["epoch"] is None or r["epoch"] < now - self.since * 86400:
                    continue
            if self.until is not None:
                if r["epoch"] is None or r["epoch"] > now - self.until * 86400:
                    continue
            out.append(r)
        return out

    def active(self):
        return bool(self.roles or self.kinds or self.model or self.provider or
                    self.since is not None or self.until is not None or self.date or
                    self.contains or self.tool or self.errors_only or self.no_tools)

def _csv(v):
    if not v: return []
    return [x.strip() for x in str(v).split(",") if x.strip()]

def describe_filters(f):
    bits = []
    if f.roles: bits.append("role=" + ",".join(f.roles))
    if f.kinds: bits.append("kind=" + ",".join(f.kinds))
    if f.model: bits.append(f"model~{f.model}")
    if f.provider: bits.append(f"provider~{f.provider}")
    if f.since is not None: bits.append(f"since={f.since}d")
    if f.until is not None: bits.append(f"until={f.until}d")
    if f.date: bits.append(f"date={f.date}")
    if f.contains: bits.append(f"contains~{f.contains!r}")
    if f.tool: bits.append(f"tool~{f.tool}")
    if f.errors_only: bits.append("errors-only")
    if f.no_tools: bits.append("no-tools")
    return (" | filters: " + "; ".join(bits)) if bits else ""
# ---------- printers / exporters (consume normalized rows + a counts dict) ----------

def _notice(msg):
    print(msg, file=sys.stderr)

def _legend(sid, harness, n, c, label):
    taglegend = ("[ok] content | [tool-call-step] tools | [tool-output] result | [empty] lost | "
                 "[thinking] CoT | [lifecycle] internal | [error] fault")
    print(f"## {label} {sid} | harness={harness} | {n} rows | {c} | {taglegend}")

def counts(rows):
    c = {"msgs": 0, "u": 0, "a_ok": 0, "a_tool": 0, "a_lost": 0, "tool": 0,
         "d": 0, "reasoning": 0, "empty": 0, "lifecycle": 0, "error": 0, "words": 0}
    for r in rows:
        c["msgs"] += 1
        c["words"] += len(r["content"] or "")
        if r["role"] == "user": c["u"] += 1
        elif r["role"] == "developer": c["d"] += 1
        elif r["role"] == "assistant":
            if r["kind"] == "tool_call": c["a_tool"] += 1
            elif r["kind"] == "message": c["a_ok"] += 1
            elif r["kind"] in ("empty", "error"): c["a_lost"] += 1
            # reasoning/tool_output/lifecycle counted in their own buckets below
        if r["kind"] == "tool_output": c["tool"] += 1
        if r["kind"] == "reasoning": c["reasoning"] += 1
        if r["kind"] == "empty": c["empty"] += 1
        if r["kind"] == "lifecycle": c["lifecycle"] += 1
        if r["kind"] == "error": c["error"] += 1
    return c

def print_transcript(rows, sid, harness, c, n=None, clipn=None):
    rows = rows if n is None else rows[:n]
    _legend(sid, harness, len(rows), c, "TRANSCRIPT")
    _emit_text(rows, clipn)

def print_tail(rows, sid, harness, c, n=5, clipn=None):
    rows = list(reversed(rows[-n:])) if n else list(reversed(rows))
    _legend(sid, harness, len(rows), c, "TAIL")
    _emit_text(rows, clipn)

def print_preview(rows, sid, harness, c, n=5, clipn=1200):
    rows = rows[:n]
    print(f"## PREVIEW {sid} | {harness} | {c}")
    for r in rows:
        b = body_of(r)
        print(f"{r['ts']} [{r['role']:9}] {kind_tag(r)} {b[:clipn]}" +
              ("\n             " + sub_of(r) if sub_of(r) else ""))
    if not rows:
        print("(no rows match current filters)")

def print_md(rows, sid, harness, c):
    rows = [r for r in rows if r["kind"] != "lifecycle"]
    print(f"# Transcript — `{sid}`  ")
    print(f"harness: `{harness}` | rows: {len(rows)} | {c}  ")
    print("tags: `[ok]` content · `[tool-call-step]` tools · `[tool-output]` result · "
          "`[empty: …]` lost · `[thinking]` CoT · `[error]` · `[lifecycle]` stripped  \n")
    for r in rows:
        b = body_of(r)
        print(f"**{r['ts']}** `[{r['role']}]` {kind_tag(r)}\n\n{b}\n")
        s = sub_of(r)
        if s: print(f"\n> _{s}_\n")

def print_jsonl(rows, sid, harness, c):
    print(f"## JSONL {sid} | {harness} | {len(rows)} messages | content=null where lost (do not fabricate)")
    for r in rows:
        print(json.dumps({
            "role": r["role"], "ts": r["ts"], "kind": r["kind"], "tag": kind_tag(r),
            "content": r["content"], "content_chars": len(r["content"] or ""),
            "reasoning_chars": len(r["reasoning"]) if r["reasoning"] else None,
            "tool_names": r["tool_names"] or None, "is_error": r["is_error"],
            "attachments": r["attachments"] or None,
            "unrecovered": r["unrecovered"] or None}, ensure_ascii=False))

def print_json(rows, sid, harness, c, info=None):
    rep = fidelity_report(rows, sid, harness)
    info = info or {}
    out = {"session": {"id": sid, "harness": harness, "source": info.get("source"),
                       "model": info.get("model"), "provider": info.get("provider"),
                       "start": info.get("start"), "end": info.get("end"),
                       "counts": c, "fidelity": rep,
                       "unrecovered": info.get("unrecovered") or [],
                       "note": info.get("note"), "usage": info.get("usage") or {}},
           "messages": [_json_msg(r) for r in rows]}
    print(json.dumps(out, ensure_ascii=False))

def _json_msg(r):
    return {"seq": r["seq"], "role": r["role"], "ts": r["ts"], "kind": r["kind"],
            "tag": kind_tag(r), "content": r["content"],
            "reasoning_chars": len(r["reasoning"]) if r["reasoning"] else None,
            "tool_names": r["tool_names"] or None, "is_error": r["is_error"],
            "attachments": r["attachments"] or None,
            "unrecovered": r["unrecovered"] or None}

def _emit_text(rows, clipn):
    for r in rows:
        b = body_of(r)
        tag = kind_tag(r)
        if clipn and b and len(b) > clipn:
            extra = len(b) - clipn
            print(f"{r['ts']} [{r['role']:9}] {tag} {b[:clipn]}\u2026 [+{extra} chars clipped]")
        elif b:
            print(f"{r['ts']} [{r['role']:9}] {tag} {b}")
        else:
            print(f"{r['ts']} [{r['role']:9}] {tag}")
        s = sub_of(r)
        if s: print(f"             {s}")
# ---------- HERMES adapter ----------
# sessions table has billing_provider (not a bare 'provider'); provider info is
# also derivable per-call in session_model_usage. Here we read billing_provider.

def hermes_list(n, source=None, since=None, sort="recent"):
    db = ro(HB)
    where, args = [], []
    if source and source != "all":
        where.append("s.source=?"); args.append(source)
    elif source is None:
        where.append("s.source!='cron'")
    if since:
        where.append("m.timestamp >= (strftime('%s','now') - ?*86400)"); args.append(since)
    order_col = "max(m.timestamp)" if sort == "recent" else "min(m.timestamp)"
    q = f"""select s.id, min(m.timestamp), max(m.timestamp),
      coalesce(s.message_count, count(*)),
      coalesce(s.model,'?'), coalesce(s.billing_provider,'?'), s.profile_name,
      sum(length(coalesce(m.content,'')))
      from sessions s join messages m on m.session_id=s.id
      {'where ' + ' and '.join(where) if where else ''}
      group by s.id order by {order_col} desc limit ?"""
    args.append(n)
    shown = db.execute(q, args).fetchall()
    print(f"## hermes sessions ({len(shown)} shown) | model|provider shown when present")
    for sid, start, end, msgs, model, prov, prof, chars in shown:
        print(f"{sid} | hermes | {ist(start)}-> {ist(end)} | msgs:{msgs} ~{chars or 0}w | "
              f"{clip(model,16)} via {clip(prov,12)} | @session:default/{sid}")

def hermes_meta(sid):
    db = ro(HB)
    r = db.execute("select model, billing_provider, source, started_at, ended_at, title from sessions where id=?", (sid,)).fetchone()
    if not r: return None
    return {"model": r[0], "provider": r[1], "source": r[2],
            "start": ist(r[3]) if r[3] else "?", "end": ist(r[4]) if r[4] else "?"}

def hermes_search(term):
    db = ro(HB)
    pat = "%" + term + "%"
    res = db.execute("select m.session_id, s.model, s.billing_provider, m.role, m.timestamp, "
                     "substr(coalesce(m.content, m.api_content, ''), 1, 200) from messages m "
                     "join sessions s on s.id = m.session_id "
                     "where (m.content like ?) or (m.tool_calls like ?) "
                     "order by m.timestamp desc limit 12", (pat, pat)).fetchall()
    for sid, model, prov, role, ts, c in res:
        print(f"{sid} | {clip(model,12)} | {clip(prov,10)} | {ist(ts)} [{role:9}] {clip(c or '',110)}")
    if not res: print("(no matches)", file=sys.stderr)

def _hermes_rows(sid):
    db = ro(HB)
    r = db.execute("select model, billing_provider from sessions where id=?", (sid,)).fetchone()
    model, provider = (r or (None, None))
    dbrows = db.execute(
        "select role, timestamp, content, reasoning_content, tool_calls, "
        "api_content, finish_reason, tool_name, effect_disposition from messages "
        "where session_id=? and role!='session_meta' order by timestamp", (sid,)).fetchall()
    if not dbrows:
        return []
    out = []
    for i, (role, ts, content, reasoning, tcs, api_content, finish, tname, eff) in enumerate(dbrows):
        names = _tool_names(tcs)
        if role == "tool":
            out.append(row("hermes", sid, i, "tool", ist(ts), "tool_output",
                           content=content, model=model, provider=provider, epoch=ts))
        elif role == "assistant":
            rc = reasoning or ""
            is_err = bool(finish and "stop" in str(finish) and eff and "error" in str(eff).lower())
            if content:
                out.append(row("hermes", sid, i, "assistant", ist(ts), "message", content=content,
                               reasoning=rc, tool_names=names, model=model, provider=provider,
                               epoch=ts, is_error=is_err))
            elif names:
                out.append(row("hermes", sid, i, "assistant", ist(ts), "tool_call", reasoning=rc,
                               content=_verbatim(tcs),
                               tool_names=names, tool_call_id=tname, model=model, provider=provider,
                               epoch=ts, sub="tool_call: " + ", ".join(clip(x,100) for x in names)))
            elif rc:
                out.append(row("hermes", sid, i, "assistant", ist(ts), "reasoning", reasoning=rc,
                               model=model, provider=provider, epoch=ts))
            elif api_content:
                out.append(row("hermes", sid, i, "assistant", ist(ts), "empty", model=model, provider=provider,
                               epoch=ts, unrecovered=[f"api_content present: {str(api_content)[:40]!r}"]))
            else:
                out.append(row("hermes", sid, i, "assistant", ist(ts), "empty", model=model, provider=provider,
                               epoch=ts, unrecovered=["content empty; no tool_calls/reasoning/api_content"]))
        else:  # user
            out.append(row("hermes", sid, i, "user", ist(ts), "message", content=content,
                           model=model, provider=provider, epoch=ts))
    return out
# ---------- OPENCODE adapter (V2: message=metadata, part=content) ----------

def _infer_provider(m):
    if not m: return None
    m = m.lower()
    if m.startswith(("gpt","o1","o3","o4")): return "openai"
    if m.startswith("claude"): return "anthropic"
    if m.startswith(("command","cmd")): return "command"
    if m.startswith("gemini"): return "google"
    return None

def _oc_modelstr(m):
    """session.model is JSON like {"id":"big-pickle","providerID":"opencode"} -> readable."""
    if not m: return "?"
    if isinstance(m, str) and m.strip().startswith("{"):
        try:
            o = json.loads(m)
            mid = o.get("id") or o.get("name") or o.get("model") or "?"
            prov = o.get("providerID") or o.get("provider") or ""
            return f"{mid} ({prov})" if prov else str(mid)
        except Exception:
            pass
    return str(m)

def opencode_list(n):
    db = ro(ODB)
    cols = [c[1] for c in db.execute("pragma table_info(session)")]
    extra = ", ".join(c for c in ("project_id","directory","agent") if c in cols)
    q = f"select id, time_updated, title, model {extra and ', '+extra} from session order by time_updated desc limit ?"
    shown = db.execute(q, (n,)).fetchall()
    print(f"## opencode sessions ({len(shown)} shown) | newest first | model|provider inferred")
    for rowv in shown:
        sid = rowv[0]; ts = ist(rowv[1]/1000.0) if rowv[1] else "?"
        model = _oc_modelstr(rowv[3] if len(rowv) > 3 else None)
        prov = _infer_provider(model)
        title = rowv[2] if len(rowv) > 2 else "-"
        print(f"{sid} | opencode | {ts} | {clip(model,20)} via {clip(prov or '?',8)} | {clip(title,40)} | @session:opencode/{sid}")

def opencode_search(term):
    db = ro(ODB)
    pat = "%" + term.replace("%", r"\%").replace("_", r"\_") + "%"
    res = db.execute("select s.id, s.model, m.time_created, p.data from part p "
                     "join session s on s.id=p.session_id join message m on m.id=p.message_id "
                     "where json_extract(p.data,'$.type')='text' "
                     "and json_extract(p.data,'$.text') like ? escape '\\' "
                     "order by m.time_created desc limit 12", (pat,)).fetchall()
    for sid, model, ts, data in res:
        txt = json.loads(data or {}); snippet = clip(txt.get("text",""), 110)
        print(f"{sid} | {_oc_modelstr(model)} | {ist(ts/1000.0)} | {snippet}")
    if not res: print("(no matches)", file=sys.stderr)

def _opencode_load(sid):
    db = ro(ODB)
    msgs = db.execute("SELECT id, time_created, data FROM message WHERE session_id=? ORDER BY time_created ASC", (sid,)).fetchall()
    if not msgs:
        return [], {"source": sid, "harness": "opencode", "note": "NOT FOUND (no messages)"}
    mrow = db.execute("select model from session where id=?", (sid,)).fetchone()
    model = _oc_modelstr(mrow and mrow[0])
    info = {"model": model, "provider": _infer_provider(model), "source": sid}
    pids = [m[0] for m in msgs]
    parts = {}
    if pids:
        q = "select message_id, data from part where message_id in (%s) order by message_id, time_created ASC" % ",".join("?"*len(pids))
        for mid, pdata in db.execute(q, pids).fetchall():
            try: parts.setdefault(mid, []).append(json.loads(pdata))
            except Exception: info.setdefault("unrecovered", []).append("1 unparseable part JSON skipped")
    out = []; seq = 0; skipped = collections.defaultdict(int)
    for mid, ts, mdata in msgs:
        md = json.loads(mdata or "{}")
        role = md.get("role", "?")
        epoch = (ts or 0) / 1000.0; t = ist(epoch)
        for p in parts.get(mid, []):
            pt = p.get("type")
            if pt in ("step-start", "step-finish", "compaction", "agent"):
                skipped[pt] += 1; continue
            if pt == "reasoning":
                out.append(row("opencode", sid, seq, role, t, "reasoning", reasoning=p.get("text"),
                               model=model, provider=info["provider"], epoch=epoch,
                               sub=f"[thinking: {len(p.get('text') or '')} chars CoT]")); seq += 1
            elif pt == "text":
                txt = (p.get("text") or "").strip()
                if txt:
                    out.append(row("opencode", sid, seq, role, t, "message", content=txt,
                                   model=model, provider=info["provider"], epoch=epoch)); seq += 1
            elif pt == "tool":
                st = p.get("state") or {}
                nm = p.get("tool") or "?"
                is_err = st.get("status") in ("error", "failed", "cancelled")
                out.append(row("opencode", sid, seq, role, t, "tool_call", content=_verbatim(st.get("input")),
                               tool_names=[nm],
                               tool_call_id=p.get("callID"), is_error=is_err, model=model,
                               provider=info["provider"], epoch=epoch,
                               sub=f"tool_call: {nm} | status={st.get('status','?')}")); seq += 1
                outv = st.get("output")
                if isinstance(outv, dict): outv = outv.get("output")
                if outv:
                    out.append(row("opencode", sid, seq, "tool", t, "tool_output", content=str(outv),
                                   model=model, provider=info["provider"], epoch=epoch)); seq += 1
            elif pt == "patch":
                out.append(row("opencode", sid, seq, role, t, "attachment", model=model, provider=info["provider"],
                               epoch=epoch, attachments=[{"type":"patch","files":len(p.get("files") or [])}],
                               content=f"[patch: {len(p.get('files') or [])} file(s), hash {clip(p.get('hash'),16)}]")); seq += 1
            elif pt == "file":
                out.append(row("opencode", sid, seq, role, t, "attachment", model=model, provider=info["provider"],
                               epoch=epoch,
                               attachments=[{"type":"file","name":p.get("filename") or "",
                                             "mime":p.get("mime") or "", "url":p.get("url","")}],
                               content=f"[file: {p.get('filename') or '?'} ({p.get('mime','')})]")); seq += 1
            else:
                skipped[pt] += 1
    for k, v in skipped.items():
        info.setdefault("unrecovered", []).append(f"{v} {k} parts skipped (no conversational text)")
    return out, info
# ---------- CLINE adapter ----------

def cline_list(n):
    db = ro(CLINEDB)
    rows = db.execute("select session_id, started_at, ended_at, model, provider, is_subagent, messages_path from sessions "
                      "order by started_at desc limit ?", (n,)).fetchall()
    print(f"## cline sessions ({len(rows)} shown) | newest first")
    for sid, s, e, model, prov, sub, mp in rows:
        size = os.path.getsize(mp) if mp and os.path.isfile(mp) else 0
        print(f"{sid} | cline | {ist_any(s)}→{ist_any(e)} | msgs path {size//1024}KB | "
              f"{clip(model or '?',16)} via {clip(prov or '?',10)} | sub={sub} | @session:cline/{sid}")

def cline_search(term):
    db = ro(CLINEDB)
    pat = "%" + term.replace("%", r"\%").replace("_", r"\_") + "%"
    rows = db.execute("select session_id, model, provider, started_at, messages_path from sessions "
                      "order by started_at desc limit 200").fetchall()
    hits = []
    for sid, model, prov, s, mp in rows:
        if not mp or not os.path.isfile(mp): continue
        try:
            d = json.load(open(mp, encoding="utf-8"))
        except Exception:
            continue
        for m in d.get("messages", []):
            text = _block_texts(m.get("content"))
            if term.lower() in text.lower():
                snippet = clip(text, 140)
                hits.append((sid, model, prov, ist_any(m.get("ts")), snippet))
                if len(hits) >= 12: break
        if len(hits) >= 12: break
    for sid, model, prov, ts, snip in hits:
        print(f"{sid} | {clip(model,16)} | {clip(prov,10)} | {ts} {snip}")
    if not hits: print("(no matches)", file=sys.stderr)

def _verbatim(v):
    """Push a raw value straight into `content`: pass strings through verbatim,
    dump dict/list as JSON, drop None/empty. Never invents text."""
    if v is None or v == "" or v == [] or v == {}: return None
    if isinstance(v, str): return v
    if isinstance(v, (dict, list)): return json.dumps(v, ensure_ascii=False)
    return str(v)

def _block_text(b):
    """Readable text of ONE Cline content block, recursing into tool_result lists.

    The old _block_texts called itself on a single block dict (it only handled
    lists) -> tool_result.content (a list of blocks) yielded ''. That silently
    dropped every structured tool result. Single-block-aware + recursive."""
    if not isinstance(b, dict): return str(b)
    t = b.get("type")
    if t in ("text", "input_text"): return b.get("text") or ""
    if t == "thinking": return b.get("thinking") or ""
    if t == "tool_use":
        inp = b.get("input")
        return inp if isinstance(inp, str) else (json.dumps(inp) if inp else "")
    if t == "tool_result":
        cr = b.get("content")
        if isinstance(cr, str): return cr
        if isinstance(cr, list): return " ".join(_block_text(x) for x in cr).strip()
        if cr is None: return ""
        return json.dumps(cr)
    if t in ("image", "image_url", "video", "audio"): return f"[{t}]" + (b.get("name") or "")
    return ""

def _block_texts(content):
    """Flatten Cline message.content (str | list | single block) into plain text."""
    if isinstance(content, str): return content
    if isinstance(content, dict): return _block_text(content)
    if isinstance(content, list): return " ".join(_block_text(b) for b in content).strip()
    return ""

def _cline_rows(sid):
    mp = None
    db = ro(CLINEDB)
    r = db.execute("select messages_path, model, provider from sessions where session_id=?", (sid,)).fetchone()
    if not r: return [], {"source": sid, "note": "NOT FOUND"}
    mp, model, prov = r
    if not mp or not os.path.isfile(mp):
        return [], {"source": sid, "model": model, "provider": prov,
                    "unrecovered": [f"messages_path missing on disk: {mp}"]}
    d = json.load(open(mp, encoding="utf-8"))
    out = []; seq = 0
    for m in d.get("messages", []):
        role = m.get("role", "?")
        ts = ist_any(m.get("ts"))
        epoch = to_epoch(m.get("ts"))
        mi = m.get("modelInfo") or {}
        text = _block_texts(m.get("content"))
        content = text.strip() if text else ""
        blocks = m.get("content") or []
        if not isinstance(blocks, list): blocks = []
        # Split typed blocks so thinking, prose, tool arguments and tool results
        # each land in the right row kind. (Was: a `think` char-count was computed
        # and never emitted, and tool_call content was dropped.)
        thinking_text = " ".join(_block_text(b) for b in blocks
                                 if isinstance(b,dict) and b.get("type")=="thinking").strip()
        args_text = " ".join(_block_text(b) for b in blocks
                             if isinstance(b,dict) and b.get("type")=="tool_use").strip()
        txt = " ".join(_block_text(b) for b in blocks
                       if isinstance(b,dict) and b.get("type") in ("text","input_text",
                                                                    "image","image_url","video","audio")).strip()
        tu = [b.get("name") for b in blocks if isinstance(b,dict) and b.get("type")=="tool_use" and b.get("name")]
        tr = [b for b in blocks if isinstance(b,dict) and b.get("type")=="tool_result"]
        tcall_id = next((b.get("id") or b.get("tool_use_id")
                         for b in blocks if isinstance(b,dict) and b.get("type")=="tool_use"), None)
        if thinking_text:
            out.append(row("cline", sid, seq, "assistant", ts, "reasoning", reasoning=thinking_text,
                           model=model or mi.get("id"), provider=prov or mi.get("provider"), epoch=epoch,
                           sub=f"[thinking: {len(thinking_text)} chars CoT]"))
            seq += 1
        if role == "tool":
            body = clip(_block_texts(tr[0].get("content")) if tr else "", 4000) if tr else ""
            out.append(row("cline", sid, seq, "tool", ts, "tool_output", content=body or None,
                           model=model or mi.get("id"), provider=prov or mi.get("provider"), epoch=epoch))
            seq += 1
        elif role == "assistant" and tu:
            out.append(row("cline", sid, seq, "assistant", ts, "tool_call", reasoning=None,
                           content=args_text or None,
                           tool_names=tu, tool_call_id=tcall_id,
                           model=model or mi.get("id"), provider=prov or mi.get("provider"), epoch=epoch,
                           sub="tool_call: " + ", ".join(clip(x,100) for x in tu)))
            seq += 1
        elif role == "assistant":
            if txt:
                out.append(row("cline", sid, seq, "assistant", ts, "message", content=txt or None,
                               reasoning=None,
                               model=model or mi.get("id"), provider=prov or mi.get("provider"),
                               epoch=epoch))
                seq += 1
        elif role == "user":
            # Cline persists tool results as role="user" messages whose blocks are
            # tool_result (linked via tool_use_id/name). Emit each as a tool_output
            # row so the result text + call association survive; only genuine
            # prompt text stays a user message. (Previously these were misfiled as
            # user messages and the structured result text was dropped.)
            if tr:
                for rb in tr:
                    rtext = _block_texts(rb.get("content")).strip()
                    out.append(row("cline", sid, seq, "tool", ts, "tool_output",
                                   content=rtext or None,
                                   tool_names=[rb.get("name")] if rb.get("name") else [],
                                   tool_call_id=rb.get("tool_use_id") or rb.get("id"),
                                   model=model or mi.get("id"), provider=prov or mi.get("provider"), epoch=epoch,
                                   sub=f"tool_result: {rb.get('name') or '?'}"))
                    seq += 1
            prompt = " ".join(_block_text(b) for b in blocks
                              if isinstance(b, dict)
                              and b.get("type") not in ("tool_use", "tool_result",
                                                        "image", "image_url", "video", "audio")).strip()
            if prompt:
                out.append(row("cline", sid, seq, "user", ts, "message", content=prompt or None,
                               model=model or mi.get("id"), provider=prov or mi.get("provider"), epoch=epoch))
                seq += 1
        else:
            out.append(row("cline", sid, seq, role, ts, "message", content=content or None,
                           model=model or mi.get("id"), provider=prov or mi.get("provider"), epoch=epoch))
            seq += 1
    return out, {"source": sid, "model": model, "provider": prov,
                 "note": f"{d.get('version','?')} file, {len(d.get('messages',[]))} msgs"}
# ---------- CODEX adapter (Responses-API rollout JSONL) ----------
# Envelope verified live (codex-cli 0.149.0): line types session_meta/event_msg/
# response_item/world_state/turn_context. response_item.payload.type in
# {message, custom_tool_call, custom_tool_call_output, reasoning}. message roles:
# developer|user|assistant|tool. model name lives in turn_context/world_state only.

def _codex_all_files():
    return sorted(glob.glob(os.path.join(CODEX_SESSION_DIR, "*", "*", "*", "rollout-*.jsonl")) +
                  glob.glob(os.path.join(CODEX_SESSION_DIR, "*", "*", "rollout-*.jsonl")))

def _codex_file(sid):
    if not sid or len(sid) < 8: return None
    for f in _codex_all_files():
        if sid in os.path.basename(f):
            return f
    return None

def codex_list(n):
    files = _codex_all_files()
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        print("(no codex sessions found under ~/.codex/sessions)"); return
    print(f"## codex sessions ({min(len(files), n)} of {len(files)}) | newest first | model from turn_context")
    for f in sorted(files, key=os.path.getmtime, reverse=True)[:n]:
        sid = started = provider = model = None; nitems = 0; tool_calls = 0
        for ln in open(f, encoding="utf-8"):
            ln = ln.strip()
            if not ln: continue
            try: o = json.loads(ln)
            except Exception: continue
            t = o.get("type")
            if t == "session_meta":
                pl = o.get("payload") or {}
                sid = pl.get("session_id") or os.path.basename(f)
                started = pl.get("started_at") or pl.get("timestamp")
                provider = pl.get("model_provider") or "openai"  # session_meta has NO model field
            elif t == "response_item":
                nitems += 1
                if (o.get("payload") or {}).get("type") == "custom_tool_call": tool_calls += 1
            elif t in ("turn_context", "world_state"):
                pl = o.get("payload") or {}
                model = model or pl.get("model") or pl.get("current_model") or (pl.get("state") or {}).get("model")
        sid = sid or os.path.basename(f)
        print(f"{sid} | codex | {ist_any(started)} | {clip(provider or 'openai',12)} "
              f"| model:{clip(model or 'unknown',16)} | items~{nitems} tools~{tool_calls} | "
              f"{(os.path.getsize(f)//1024) or 1}KB | @session:codex/{sid}")

def _codex_block_text(b):
    if not isinstance(b, dict): return ""
    if b.get("type") in ("text", "input_text"): return b.get("text","") or ""
    if b.get("type") == "output_text": return b.get("text","") or b.get("output_text","") or ""
    if b.get("type") == "summary":
        return "".join(x.get("delta","") for x in (b.get("summary") or []) if isinstance(x, dict))
    return ""

def _codex_rows(sid):
    p = _codex_file(sid)
    if not p: return [], {"source": sid, "harness": "codex", "note": "NOT FOUND (no rollout-*.jsonl names this id)"}
    rows = []; seq = 0; unrec = []
    model = None; provider = None
    for ln in open(p, encoding="utf-8"):
        ln = ln.strip()
        if not ln: continue
        try: o = json.loads(ln)
        except Exception: unrec.append("1 unparseable JSON line skipped"); continue
        t = o.get("type"); pl = o.get("payload")
        if not isinstance(pl, dict): continue
        epoch = to_epoch(o.get("timestamp"))
        ts = ist(epoch) if epoch is not None else "?"
        if t in ("world_state", "turn_context"):
            model = model or pl.get("model") or pl.get("current_model") or (pl.get("state") or {}).get("model")
            # lifecycle/config rows: emitted so --include-lifecycle can show them, stripped by default
            rows.append(row("codex", sid, seq, "meta", ts, "lifecycle",
                            content=clip(json.dumps(pl)[:200], 200), epoch=epoch)); seq += 1
            continue
        if t == "session_meta" or t == "event_msg": continue
        if t == "response_item" and pl.get("type") == "reasoning":
            rows.append(row("codex", sid, seq, "assistant", ts, "reasoning", reasoning=None,
                            model=model, provider=provider, epoch=epoch,
                            sub="[thinking: (encrypted CoT, not a reply)")); seq += 1; continue
        if t != "response_item":
            rows.append(row("codex", sid, seq, "meta", ts, "lifecycle", content=clip(json.dumps(pl)[:200], 200), epoch=epoch)); seq += 1
            continue
        ptype = pl.get("type")
        if ptype == "custom_tool_call":
            nm = pl.get("name") or "?"
            rows.append(row("codex", sid, seq, "assistant", ts, "tool_call", content=_verbatim(pl.get("input")),
                            tool_names=[nm],
                            tool_call_id=pl.get("call_id"), model=model, provider=provider, epoch=epoch,
                            sub=f"tool_call: {nm}")); seq += 1
        elif ptype == "custom_tool_call_output":
            out = pl.get("output", [])
            txt = " ".join(_codex_block_text(b) for b in out if isinstance(b, dict)).strip()
            rows.append(row("codex", sid, seq, "tool", ts, "tool_output", content=txt or None,
                            model=model, provider=provider, epoch=epoch)); seq += 1
        elif ptype == "message":
            role = pl.get("role"); blocks = pl.get("content") or []
            if isinstance(blocks, str): blocks = [{"type":"text","text":blocks}]
            think = sum(len(_codex_block_text(b)) for b in blocks if b.get("type") in ("reasoning","thinking"))
            txt = " ".join(_codex_block_text(b) for b in blocks).strip()
            if role == "assistant" and txt:
                rows.append(row("codex", sid, seq, "assistant", ts, "message", content=txt,
                                model=model, provider=provider, epoch=epoch)); seq += 1
            elif role == "assistant" and think:
                rows.append(row("codex", sid, seq, "assistant", ts, "reasoning", reasoning=txt or None,
                                model=model, provider=provider, epoch=epoch,
                                sub=f"[thinking: {think} chars CoT]")); seq += 1
            elif role == "assistant" and pl.get("tool_calls"):
                rows.append(row("codex", sid, seq, "assistant", ts, "tool_call",
                                tool_names=[b.get("name") or "?" for b in blocks if isinstance(b,dict) and b.get("type")=="tool_use"],
                                model=model, provider=provider, epoch=epoch)); seq += 1
            elif role == "assistant":
                rows.append(row("codex", sid, seq, "assistant", ts, "message", content=None,
                                model=model, provider=provider, epoch=epoch,
                                unrecovered=["assistant message with no text/tool_calls"])); seq += 1
            elif role in ("developer", "system", "user"):
                rows.append(row("codex", sid, seq, role, ts, "message", content=txt or None,
                                model=model, provider=provider, epoch=epoch)); seq += 1
            else:
                rows.append(row("codex", sid, seq, role or "?", ts, "message", content=txt or None,
                                model=model, provider=provider, epoch=epoch)); seq += 1
        else:
            unrec.append(f"response_item.payload.type={ptype} not normalized")
            rows.append(row("codex", sid, seq, "meta", ts, "lifecycle", content=f"[{ptype}]",
                            model=model, provider=provider, epoch=epoch)); seq += 1
    # backfill model/provider to rows that lacked it
    if model and any(r["model"] is None for r in rows):
        for r in rows:
            if r["model"] is None: r["model"], r["provider"] = model, provider
    info = {"source": sid, "harness": "codex", "model": model, "provider": provider or "openai"}
    if unrec: info["unrecovered"] = unrec
    return rows, info

def codex_search(term):
    """Search assistant/tool text in codex rollouts (substring, case-insensitive)."""
    hits = []
    for f in _codex_all_files():
        bn = os.path.basename(f)
        sid_f = bn[:-6] if bn.endswith(".jsonl") else bn
        if sid_f.startswith("rollout-"): sid_f = sid_f[8:]
        st = None
        try: fh = open(f, errors="replace")
        except OSError: continue
        for line in fh:
            line = line.strip()
            if not line: continue
            try: o = json.loads(line)
            except Exception: continue
            t = o.get("type")
            if t == "session_meta":
                sid_f = o.get("session_id") or o.get("id") or sid_f; st = o.get("timestamp")
            elif t == "response_item":
                payload = o.get("payload") or {}
                for blk in (payload.get("content") or []):
                    txt = blk.get("text", "") if isinstance(blk, dict) else ""
                    if isinstance(txt, str) and term.lower() in txt.lower():
                        hits.append((sid_f, ist_any(st), clip(txt, 110)))
        fh.close()
    for sid_f, ts, snip in hits[:12]:
        print(f"{sid_f} | codex | {ts} | {snip}")
    if not hits: print("(no matches)", file=sys.stderr)
# ---------- AGY adapter ----------
# PRIMARY source: the plaintext transcript at
#   ~/.gemini/antigravity-cli/brain/<conversationId>/.system_generated/logs/transcript_full.jsonl
# Spec: SpecStory "getspecstory" ANTIGRAVITY-FORMAT.md (reverse-engineered, v1.1.5+).
# One JSON object per step: step_index, source(USER_EXPLICIT|SYSTEM|MODEL),
# type(USER_INPUT|PLANNER_RESPONSE|GENERIC|SYSTEM_MESSAGE|CHECKPOINT),
# status(DONE|RUNNING|ERROR|INVALID), created_at(RFC3339 UTC Z),
# content, tool_calls[{name,args,toolAction,toolSummary}], thinking.
# Model (session-level) from brain/<id>/settings.json "model".
# Token usage from the SQLite gen_metadata protobuf blobs (verified field map,
# matches _agy_gen_meta / AgentsView-SessKit-SpecStory: field 1 -> field 19 =
# model_id; field 1 -> field 4 = usage { field 2 = uncached input, field 3 =
# output tokens (already includes thinking), field 5 = cache_read (absent when
# 0) }. cache_read is reported separately and is NEVER summed into input.
#
# FALLBACK (only when no brain transcript): the per-conversation DB at
# conversations/<id>.db with protobuf `step_payload` string-extraction +
# gen_metadata model/tokens. Fidelity is reported explicitly; never fabricated.

AGY_BRAIN = os.path.expanduser("~/.gemini/antigravity-cli/brain")
AGY_DIR = os.path.expanduser("~/.gemini/antigravity-cli/conversations")
AGY_LOG = os.path.expanduser("~/.gemini/antigravity-cli/log")

_AGY_REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
_AGY_META = re.compile(r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?(</\1>)", re.S)

def _tool_names(tcs):
    """Normalize a JSON tool-call array/string into a list of invoked tool names.
    Shared across adapters (hermes/opencode/cline/codex). Tolerant of schemas:
    handles {name}, {type}, {tool_name}, {function:{name}}, and bare strings."""
    if not tcs: return []
    try:
        arr = json.loads(tcs) if isinstance(tcs, (str, bytes)) else tcs
    except Exception:
        return []
    if not isinstance(arr, list): arr = [arr]
    names = []
    for t in arr:
        if isinstance(t, str):
            names.append(t); continue
        nm = (t.get("name") or (t.get("function") or {}).get("name")
              or t.get("tool_name") or (t.get("type") if not t.get("type") in ("function","tool") else None)
              or t.get("type") or "?")
        names.append(nm)
    return names
_AGY_TOOL_RE = re.compile(r'"toolAction"\s*:\s*"([^"]+)"')
_AGY_CALLID_RE = re.compile(r'call_\d+')

def _agy_transcript(path):
    """Return (steps, unrec) from a transcript_full.jsonl, or None if absent."""
    if not os.path.isfile(path):
        return None
    out = []; unrec = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for i, ln in enumerate(f, 1):
            ln = ln.strip()
            if not ln: continue
            try: out.append(json.loads(ln))
            except json.JSONDecodeError as e:
                unrec.append(f"line {i}: malformed jsonl ({e.msg})")
    return out, unrec

def _agy_settings_model(sid):
    p = os.path.join(AGY_BRAIN, sid, "settings.json")
    try:
        return json.load(open(p)).get("model") or None
    except Exception:
        return None

def _agy_user_prompt(content):
    m = _AGY_REQ.search(content or "")
    base = m.group(1) if m else (content or "")
    return _AGY_META.sub("", base).strip()

def agy_list(n):
    entries = []
    for bd in glob.glob(os.path.join(AGY_BRAIN, "*")):
        if not os.path.isdir(bd): continue
        sid = os.path.basename(bd)
        tp = os.path.join(bd, ".system_generated", "logs", "transcript_full.jsonl")
        mtime = os.path.getmtime(tp) if os.path.isfile(tp) else os.path.getmtime(bd)
        model = _agy_settings_model(sid) or "?"
        nsteps = 0
        if os.path.isfile(tp):
            with open(tp, encoding="utf-8", errors="replace") as f:
                nsteps = sum(1 for ln in f if ln.strip())
        entries.append((mtime, sid, model, nsteps, "transcript"))
    # DB-only fallback (no brain dir)
    for dbp in glob.glob(os.path.join(AGY_DIR, "*.db")):
        sid = os.path.basename(dbp)[:-3]
        if os.path.isdir(os.path.join(AGY_BRAIN, sid)): continue
        model = "?"; nsteps = 0
        try:
            db = ro(dbp)
            nsteps = db.execute("select count(*) from steps").fetchone()[0]
            gm = _agy_gen_meta(db)
            if gm and gm[0]: model = gm[0]
            db.close()
        except Exception:
            pass
        entries.append((os.path.getmtime(dbp), sid, model, nsteps, "db"))
    entries.sort(key=lambda x: -x[0])
    print(f"## agy conversations ({len(entries)} found) | "
          f"brain transcript_full.jsonl is primary; sqlite db is fallback")
    for mtime, sid, model, nsteps, src in entries[:n]:
        print(f"{sid} | agy | {ist(mtime)} | steps:{nsteps} | model: {model} | "
              f"source:{src} | @session:agy/{sid}")

def agy_search(term):
    """Full-text search across every brain transcript_full.jsonl (no longer protobuf-blind)."""
    brains = [bd for bd in glob.glob(os.path.join(AGY_BRAIN, "*")) if os.path.isdir(bd)]
    print(f"## agy search {term!r} ({len(brains)} sessions scanned)")
    hits = 0
    low = term.lower()
    for bd in brains:
        sid = os.path.basename(bd)
        tp = os.path.join(bd, ".system_generated", "logs", "transcript_full.jsonl")
        if not os.path.isfile(tp): continue
        matches = 0
        with open(tp, encoding="utf-8", errors="ignore") as f:
            for ln in f:
                if low in ln.lower(): matches += 1
        if matches:
            hits += 1
            print(f"@session:agy/{sid}  matches:{matches}  {ist(os.path.getmtime(tp))}")
    if not hits: print("(no matches)", file=sys.stderr)
    _notice(f"(agy: {len(brains)} sessions scanned; protobuf step_payload not searched)")

def agy_logs(n):
    logs = sorted(glob.glob(os.path.join(AGY_LOG, "cli-*.log")), key=os.path.getmtime, reverse=True)
    for p in logs[:n]:
        print(f"{os.path.basename(p)}  {os.path.getsize(p)//1024}KB  {ist(os.path.getmtime(p))}")
    print("(gRPC/LSP CLI logs — timing/evidence, not transcripts)")

# ---------- protobuf wire decoder (stdlib only) for gen_metadata fallback ----------
def _pb(data, depth=0):
    """Decode protobuf wire format -> [(fno, wire, value)] or None on any ambiguity.
    value is int(varint) | str(bytes) | list(nested). None => treat length-delimited as string."""
    if depth > 32 or not data: return None
    out = []; i = 0; n = len(data)
    while i < n:
        tag = 0; sh = 0
        while i < n:
            b = data[i]; i += 1; tag |= (b & 0x7f) << sh
            if not (b & 0x80): break
            sh += 7
            if sh >= 64: return None
        if tag == 0: return None
        fno = tag >> 3; w = tag & 7
        if fno == 0: return None
        if w == 0:  # varint
            v = 0; s = 0
            while i < n:
                b = data[i]; i += 1; v |= (b & 0x7f) << s
                if not (b & 0x80): break
                s += 7
                if s >= 64: return None
            out.append((fno, "varint", v))
        elif w == 2:  # length-delimited
            ln = 0; s = 0
            while i < n:
                b = data[i]; i += 1; ln |= (b & 0x7f) << s
                if not (b & 0x80): break
                s += 7
                if s >= 64: return None
            if i + ln > n: return None
            seg = data[i:i + ln]; i += ln
            sub = _pb(seg, depth + 1)
            if sub is not None:
                out.append((fno, "msg", sub))
            else:
                out.append((fno, "bytes", seg.decode("utf-8", "ignore").rstrip("\x00")))
        elif w == 1:
            if i + 8 > n: return None
            out.append((fno, "fixed64", int.from_bytes(data[i:i + 8], "little"))); i += 8
        elif w == 5:
            if i + 4 > n: return None
            out.append((fno, "fixed32", int.from_bytes(data[i:i + 4], "little"))); i += 4
        else:
            return None
    return out

def _pb_find(fields, fno, wt):
    for f in fields or []:
        if f[0] == fno and f[1] == wt: return f[2]
    return None

def _agy_gen_meta(db):
    """Decode Gemini generation metadata from gen_metadata protobuf blobs.
    Schema (verified on live data; matches SpecStory/AgentsView field map):
      top field 1 (msg) -> field 19 (model_id); field 4 (usage msg):
        field 2 = uncached input tokens, field 5 = cache_read (absent if 0),
        field 3 = total output tokens (already includes thinking).
    Per-generation rows (not a session-total blob); summed. Gated rows (missing
    input/output fields, or that fail the plausibility cap) are counted out, never
    summed; their count is surfaced as session.usage.gen_skipped so nothing is
    silently lost."""
    CAP = 2_000_000
    model = None; inp = cch = out = steps = 0
    try:
        for (blob,) in db.execute("select data from gen_metadata where data is not null"):
            if not blob: continue
            f = _pb(bytes(blob))
            if not f: continue
            f1 = _pb_find(f, 1, "msg")
            if not f1: continue
            mid = _pb_find(f1, 19, "bytes")
            if mid: model = mid
            u = _pb_find(f1, 4, "msg")
            if not u: continue
            i = _pb_find(u, 2, "varint"); o = _pb_find(u, 3, "varint")
            if i is None or o is None or i > CAP or o > CAP: continue
            c = _pb_find(u, 5, "varint")
            steps += 1; inp += i; out += o
            if c is not None and c <= CAP: cch += c
    except Exception:
        pass
    if not steps: return None
    try:
        blobs = db.execute("select count(*) from gen_metadata where data is not null").fetchone()[0]
    except Exception:
        blobs = steps
    usage = {"input_tokens": inp, "cache_read_tokens": cch,
             "output_tokens": out, "gen_steps": steps}
    if blobs > steps:
        usage["gen_skipped"] = blobs - steps  # gated out (missing fields or plausibility cap); not summed
    return model, usage

def _pb_strings(blob, minlen=8):
    """Extract contiguous printable runs from a protobuf-ish BLOB (DB fallback)."""
    if not blob: return []
    try:
        s = blob.decode("utf-8", "ignore") if isinstance(blob, (bytes, bytearray)) else str(blob)
    except Exception:
        s = ""
    out = []; cur = []
    for ch in s:
        if ch.isprintable() or ch in "\n\r\t":
            cur.append(ch)
        else:
            if len("".join(cur)) >= minlen: out.append("".join(cur))
            cur = []
    if len("".join(cur)) >= minlen: out.append("".join(cur))
    return out

_AGY_BRIEF = 2000

# Antigravity step `type` enum (spec §3.2) -> which tool call answers it.
# A tool result's type is derived from the tool category, so the pairing is
# recoverable from the type alone; GENERIC has no dedicated type and is
# matched against the oldest unmatched call.
_AGY_TOOL_RESULT_TOOLS = {
    "RUN_COMMAND": ("run_command",),
    "VIEW_FILE": ("view_file",),
    "GREP_SEARCH": ("grep_search",),
    "LIST_DIRECTORY": ("list_dir",),
    "FIND": ("find_by_name",),
    "INVOKE_SUBAGENT": ("invoke_subagent",),
    "MCP_TOOL": ("call_mcp_tool", "call_mcp"),
    "CODE_ACTION": ("write_to_file", "replace_file_content",
                    "multi_replace_file_content"),
    "GENERIC": (),
}
_AGY_TOOL_RESULT_TYPES = frozenset(_AGY_TOOL_RESULT_TOOLS)

def _agy_rows_from_transcript(sid, steps, unrec, tp):
    """Map a plaintext transcript_full.jsonl to normalized rows (PRIMARY path)."""
    # Model + token usage come from settings.json / gen_metadata (once,
    # not per-step) -- the transcript has no per-step model. Token field map
    # (field 1->{19 model, 4->usage{2 input,3 output,5 cache_read}}).
    model = _agy_settings_model(sid)
    usage = None
    if not model:
        try:
            db = ro(os.path.join(AGY_DIR, sid + ".db"))
            gm = _agy_gen_meta(db); db.close()
            if gm:
                model = gm[0]; usage = gm[1]
        except Exception:
            pass
    model = model or ""
    rows = []; pending = []  # unmatched tool-call names, oldest first
    first_epoch = None; first_ts = "?"

    def R(role, kind, content=None, **kw):
        sub = kw.pop("sub", "agy")
        return row("agy", sid, seq, role, ts, kind, content=content, epoch=e,
                   model=model, is_error=kw.pop("is_error", iserr), sub=sub, **kw)

    # Step type map. The `type` field is a closed enum (spec §3.2); `source`
    # is a second, independent signal: MODEL = model turn or tool result,
    # SYSTEM = injected scaffolding, USER_EXPLICIT = the human. Pairing a
    # result to its call is done by TOOL_RESULT_TOOLS below, so a RUN_COMMAND
    # result is attributed to the run_command call, not to "whatever call
    # happened to be oldest".
    for i, d in enumerate(steps):
        if not isinstance(d, dict):
            unrec.append(f"step {i}: non-object line"); continue
        seq = d.get("step_index", i)
        ts = ist_any(d.get("created_at")); e = to_epoch(d.get("created_at"))
        if first_epoch is None: first_epoch = e; first_ts = ts
        t = d.get("type"); st = d.get("status", "?")
        src = d.get("source", "")
        content = d.get("content") or ""
        tc = d.get("tool_calls") or []
        thinking = d.get("thinking") or ""
        iserr = st in ("ERROR", "INVALID")
        if t == "USER_INPUT" or src == "USER_EXPLICIT":
            rows.append(R("user", "message", content=_agy_user_prompt(content),
                          sub=f"USER_INPUT status={st}"))
        elif t == "PLANNER_RESPONSE":
            if thinking:
                rows.append(R("assistant", "reasoning", reasoning=thinking,
                              sub=f"tool_call: thinking ({len(thinking)}c)"))
            if content:
                rows.append(R("assistant", "message", content=content, sub=f"PLANNER_RESPONSE {st}"))
            for c in tc:
                nm = c.get("name")
                args = c.get("args") or {}
                rows.append(R("assistant", "tool_call",
                              content=json.dumps(args, ensure_ascii=False) or None,
                              tool_names=[nm] if nm else [], tool_call_id=c.get("id"),
                              sub=f"tool_call: {nm} | args={','.join(args.keys()) or 'none'}"))
                pending.append(nm)
            if not content and not tc and not thinking:
                rows.append(R("assistant", "empty", content=None,
                              sub=f"PLANNER_RESPONSE {st} (empty)"))
        elif t in _AGY_TOOL_RESULT_TYPES or (src == "MODEL" and t not in
                                             ("PLANNER_RESPONSE", "USER_INPUT")):
            # A tool result: pair it with the call it answers. Prefer the
            # type-specific tool name; fall back to the oldest unmatched
            # call (GENERIC results carry no tool identity of their own).
            want = _AGY_TOOL_RESULT_TOOLS.get(t)
            nm = None
            if want:
                for cand in want:
                    if cand in pending:
                        nm = cand; pending.remove(cand); break
            if nm is None and pending:
                nm = pending.pop(0)
            if content:
                rows.append(R("tool", "tool_output", content=content,
                              tool_names=[nm] if nm else [],
                              sub=f"{t} result" + (f" ({nm})" if nm else " (unpaired)")))
            else:
                rows.append(R("tool", "empty", content=None, sub=f"{t} {st} (empty)"))
        elif t == "SYSTEM_MESSAGE" or t == "EPHEMERAL_MESSAGE":
            rows.append(R("developer", "message", content=content or None,
                          sub=f"{t} status={st}"))
        elif t == "CHECKPOINT" or t == "CONVERSATION_HISTORY":
            rows.append(R("system", "lifecycle", content=content or None,
                          sub=f"{t} status={st}"))
        elif t == "ERROR_MESSAGE":
            rows.append(R("system", "error", content=content or None,
                          is_error=True, sub=f"ERROR_MESSAGE status={st}"))
        else:
            # Unknown type: keep the bytes, mark the gap, never guess.
            unrec.append(f"step {seq}: unknown type {t!r} (source={src!r})")
            rows.append(R("system", "lifecycle", content=content or None,
                          sub=f"{t} {st} (unknown type)"))

    info = {"harness": "agy", "source": tp, "model": model or None,
            "usage": usage,
            "note": "plaintext transcript_full.jsonl (structured): tool calls/args, "
                    "thinking, status, RFC3339 timestamps recovered"}
    if first_epoch is not None:
        info["start"] = first_ts
        info["end"] = ist_any(steps[-1].get("created_at")) if steps else first_ts
    if rows and unrec:
        rows[0]["unrecovered"] = list(rows[0].get("unrecovered") or []) + unrec
    elif unrec and not rows:
        rows = [row("agy", sid, 0, "system", first_ts, "error", content=None,
                     epoch=first_epoch, sub="agy transcript malformed",
                     unrecovered=[f"session-level: {len(unrec)} parse failures"])]
    return rows, info

def _agy_rows_from_db(sid):
    """DB-only fallback (no brain transcript): protobuf string-extraction + gen_metadata."""
    p = os.path.join(AGY_DIR, sid if sid.endswith(".db") else sid + ".db")
    if not os.path.isfile(p):
        return [], {"source": sid, "harness": "agy",
                    "note": "NOT FOUND (no brain transcript or *.db for this id)"}
    db = ro(p)
    info = {"source": os.path.basename(p), "harness": "agy",
            "note": "database fallback (no brain transcript); protobuf step_payload "
                    "string-recovered (not schema-decoded)"}
    try:
        gm = _agy_gen_meta(db)
        if gm:
            if gm[0]: info["model"] = gm[0]
            info["usage"] = gm[1]
    except Exception:
        pass
    rows = []; unrec = []; binary = 0
    try:
        dbrows = db.execute(
            "select idx, step_type, step_payload, metadata, render_info, task_details "
            "from steps order by idx").fetchall()
    except Exception as e:
        return [], {"source": sid, "harness": "agy", "note": f"db read error: {e}"}
    for idx, stype, payload, metadata, render, taskd in dbrows:
        parts = []
        for blob in (payload, metadata, render, taskd):
            if blob:
                binary += max(0, len(blob))
                parts.extend(_pb_strings(blob))
        text = " ".join(parts).strip()
        tm = os.path.getmtime(p)
        if _AGY_TOOL_RE.search(text):
            nm = _AGY_TOOL_RE.search(text).group(1)
            rows.append(row("agy", sid, idx, "assistant", ist(tm), "tool_call",
                            content=None, tool_names=[nm], epoch=tm,
                            sub=f"tool_call: {nm} | step_type={stype}"))
        elif text and ("{" in text and "}" in text) and "toolAction" not in text:
            rows.append(row("agy", sid, idx, "tool", ist(tm), "tool_output",
                            content=clip(text, _AGY_BRIEF), epoch=tm, sub=f"step_type={stype}"))
        elif text:
            role = "user" if idx == 0 else "assistant"
            rows.append(row("agy", sid, idx, role, ist(tm), "message",
                            content=clip(text, _AGY_BRIEF), epoch=tm,
                            sub=f"step_type={stype} (pb-recovered)"))
        else:
            unrec.append(f"step {idx}: {len(blob or b'')} bytes, no readable text")
    if rows and binary:
        rows[0]["unrecovered"] = list(rows[0].get("unrecovered") or []) + [
            f"agy protobuf: {binary} bytes across {len(unrec)} non-text steps not schema-decoded (text recovered via string scan only)"]
    db.close()
    return rows, info

def _agy_rows(sid):
    tp = os.path.join(AGY_BRAIN, sid, ".system_generated", "logs", "transcript_full.jsonl")
    res = _agy_transcript(tp)
    if res is not None:
        return _agy_rows_from_transcript(sid, res[0], res[1], tp)
    # fallback: protobuf DB
    return _agy_rows_from_db(sid)
# ---------- dispatch + argparse + entry point ----------
# NOTE: the harness positional is dest="harness" (NOT a "tool" attribute) so it
# never collides with the --tool content filter.

def _get_rows(tool, sid):
    """Fetch normalized rows + info for a session id."""
    if tool == "hermes":
        rows = _hermes_rows(sid)
        info = {"harness": "hermes", "source": sid}
        if (m := hermes_meta(sid)): info.update(m)
    elif tool == "opencode": rows, info = _opencode_load(sid)
    elif tool == "cline": rows, info = _cline_rows(sid)
    elif tool == "codex": rows, info = _codex_rows(sid)
    elif tool == "agy": rows, info = _agy_rows(sid)
    else: rows, info = [], {}
    return rows, info

def _peek(tool, sid):
    rows, info = _get_rows(tool, sid)
    c = counts(rows)
    print(f"## peek {sid} | harness: {info.get('harness', tool)}")
    for k in ("model", "provider", "source", "start", "end", "usage", "note", "fidelity_note"):
        if info.get(k): print(f"{k:12}: {info[k]}")
    if rows:
        print(f"range:        {rows[0]['ts']} -> {rows[-1]['ts']}")
    print(f"msgs:         {c['msgs']}  (u:{c['u']} a-ok:{c['a_ok']} a-tool:{c['a_tool']} "
          f"tool-out:{c['tool']} reasoning:{c['reasoning']} d:{c['d']} empty:{c['empty']} "
          f"lifecycle:{c['lifecycle']} errors:{c['error']} lost:{c['a_lost']})")
    print(f"content chars: ~{c['words']}")
    fr = fidelity_report(rows)
    if fr["unrecovered"]:
        print(f"fidelity:       {'complete' if fr['complete'] else 'gaps reported'}")
        for u in fr["unrecovered"]: print(f"    - {u}")
    else:
        print(f"fidelity:       complete (no gaps)")
    if rows:
        fu = clip(next((r["content"] for r in rows if r["role"] == "user" and r["content"]), ''), 120)
        print(f"first user:   {fu or '(none)'}")
    print("next: --transcript/--md/--jsonl/--json ID | --clip N | --no-tools | --role/--kind/--model/--provider/--errors")

def _load_rows(tool, sid, filt):
    rows, info = _get_rows(tool, sid)
    rows = filt.apply(rows)
    fr = fidelity_report(rows)
    for u in info.get("unrecovered", []) + fr["unrecovered"]:
        _notice(f"fidelity {sid}: {u}")
    return rows, info

def _render(tool, sid, action, a, filt):
    rows, info = _load_rows(tool, sid, filt)
    c = counts(rows)
    cl = getattr(a, "clip", None)
    if action == "transcript":
        n = int(a.transcript[1]) if len(a.transcript) > 1 else None
        print_transcript(rows, sid, tool, c, n=n, clipn=cl)
    elif action == "tail":
        n = int(a.tail[1]) if len(a.tail) > 1 else 5
        print_tail(rows, sid, tool, c, n=n, clipn=cl)
    elif action == "preview":
        n = int(a.preview[1]) if len(a.preview) > 1 else 5
        print_preview(rows, sid, tool, c, n=n, clipn=cl or 1200)
    elif action == "md":
        print_md(rows, sid, tool, c)
    elif action == "jsonl":
        print_jsonl(rows, sid, tool, c)
    elif action == "json":
        print_json(rows, sid, tool, c, info)
    elif action == "export":
        _export(rows, sid, tool, a, c, info)

def _export(rows, sid, tool, a, c, info=None):
    if not a.out:
        _notice("--export requires --out FILE [--format md|jsonl|json|txt]"); return
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        if a.format == "md": print_md(rows, sid, tool, c)
        elif a.format == "jsonl": print_jsonl(rows, sid, tool, c)
        elif a.format == "json": print_json(rows, sid, tool, c, info)
        elif a.format == "txt":
            for r in rows:
                b = body_of(r)
                print(f"{r['ts']} [{r['role']:9}] {kind_tag(r)} {b}" +\
                      ("\n             " + sub_of(r) if sub_of(r) else ""))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())
    _notice(f"exported {len(rows)} rows to {a.out} ({a.format})")

def _do_search(tool, term, filt):
    if tool == "all":
        for h in ("hermes", "opencode", "cline", "codex", "agy"):
            print(f"## -- search {term!r} in {h} --")
            try: _do_search(h, term, filt)
            except Exception as e: _notice(f"(search in {h} failed: {e})")
        _notice("[agy: protobuf step_payload not searched; brain transcripts are searched]")
        return
    if tool == "hermes": hermes_search(term)
    elif tool == "opencode": opencode_search(term)
    elif tool == "cline": cline_search(term)
    elif tool == "codex": codex_search(term)
    elif tool == "agy": agy_search(term)

def _do_list(tool, n, a):
    if tool == "all":
        for h in HARNESSES:
            try:
                print(f"\n## {h} ##")
                _do_list(h, n, a)
            except Exception as e: _notice(f"({h}: {e})")
        return
    src = a.source
    if tool == "hermes": hermes_list(n, src, a.since, a.sort)
    elif tool == "opencode": opencode_list(n)
    elif tool == "cline": cline_list(n)
    elif tool == "codex": codex_list(n)
    elif tool == "agy": agy_list(n)

def main():
    ap = argparse.ArgumentParser(description="Universal AI conversation inspector.")
    ap.add_argument("harness", choices=HARNESSES + ["all"])
    ap.add_argument("--list", type=int, nargs="?", const=20)
    ap.add_argument("--recent", type=int, nargs="?", const=8)
    ap.add_argument("--source", choices=["telegram","cli","subagent","cron","all"])
    ap.add_argument("--since", type=int, metavar="DAYS")
    ap.add_argument("--until", type=int, metavar="DAYS")
    ap.add_argument("--date", metavar="YYYY-MM-DD")
    ap.add_argument("--sort", choices=["recent","start"], default="recent")
    ap.add_argument("--search")
    ap.add_argument("--logs", type=int, metavar="N", help="agy: show N latest run log files")
    ap.add_argument("--tail", nargs="+", metavar=("ID","N"))
    ap.add_argument("--transcript", nargs="+", metavar=("ID","N"))
    ap.add_argument("--preview", nargs="+", metavar=("ID","N"))
    ap.add_argument("--md")
    ap.add_argument("--jsonl")
    ap.add_argument("--json")
    ap.add_argument("--export", metavar="ID")
    ap.add_argument("--out", metavar="FILE")
    ap.add_argument("--format", choices=["md","jsonl","json","txt"], default="jsonl")
    ap.add_argument("--peek")
    # composable filters
    ap.add_argument("--role", help="comma list: user,assistant,tool,system,developer,meta")
    ap.add_argument("--kind", help="comma list: message,tool_call,tool_output,reasoning,attachment,lifecycle,empty,error")
    ap.add_argument("--model", help="model name substring")
    ap.add_argument("--provider", help="provider substring")
    ap.add_argument("--contains", help="content substring")
    ap.add_argument("--tool", help="tool name substring")
    ap.add_argument("--errors", action="store_true")
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--include-lifecycle", action="store_true")
    ap.add_argument("--clip", type=int, metavar="N", help="truncate display body to N chars (explicit marker)")
    a = ap.parse_args()
    filt = Filt(a)
    if filt.active(): _notice(describe_filters(filt))
    try:
        if a.logs is not None:
            agy_logs(a.logs)
        elif a.list is not None or a.recent is not None:
            _do_list(a.harness, a.list if a.list is not None else a.recent, a)
        elif a.search:
            _do_search(a.harness, a.search, filt)
        elif a.peek:
            if a.harness == "all": _notice("--peek requires a single harness"); return
            _peek(a.harness, a.peek)
        elif a.tail:
            _render(a.harness, a.tail[0], "tail", a, filt)
        elif a.transcript:
            _render(a.harness, a.transcript[0], "transcript", a, filt)
        elif a.preview:
            _render(a.harness, a.preview[0], "preview", a, filt)
        elif a.md:
            _render(a.harness, a.md, "md", a, filt)
        elif a.jsonl:
            _render(a.harness, a.jsonl, "jsonl", a, filt)
        elif a.json:
            _render(a.harness, a.json, "json", a, filt)
        elif a.export:
            _render(a.harness, a.export, "export", a, filt)
        else:
            _do_list(a.harness, 8, a)
    except FileNotFoundError as e:
        print(f"db/directory not found: {e.filename or e}", file=sys.stderr); sys.exit(1)
    except sqlite3.Error as e:
        print("db error:", e, file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()
