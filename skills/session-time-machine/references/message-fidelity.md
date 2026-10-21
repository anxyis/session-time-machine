---
title: Diagnosing message fidelity in hermes state.db
---

# Message fidelity diagnostic (hermes `state.db`)

> Automated: `python3 scripts/sessions.py hermes --peek ID` (fidelity counts) and
> `--jsonl ID` (`content=null` where lost, never fabricated). This page is the raw-SQL
> forensic fallback for when you need to inspect `state.db` directly.

## Why this exists
A naive dump of `messages.content` shows many assistant rows as blank. New agents assume data loss and either fabricate a reply or drop the step, destroying the agentic chain. Those rows are almost always **assistant tool-call steps** — their headline `content` is intentionally empty, with the action in `tool_calls` and (optionally) chain-of-thought in `reasoning_content`. This recipe classifies every row of a session so you never guess.

## One query that tells you everything
```sql
select role,
  count(*)                                 total,
  sum(case when content!=''   then 1 else 0 end) content_ok,
  sum(case when content=''    then 1 else 0 end) content_empty,
  sum(case when tool_calls!='' then 1 else 0 end) with_tool_call,
  sum(case when reasoning_content!='' then 1 else 0 end) with_reasoning,
  sum(case when api_content!='' then 1 else 0 end) with_api
from messages where session_id=<sid> and role!='session_meta' group by role;
```

## How to read it
- **`a-tool`** (assistant + empty content + non-empty tool_calls): a tool-call step. The action is the function name parsed from `tool_calls`; show that. `reasoning_content` is CoT — label it `[thinking:N — CoT, not the reply]`, never render it as the reply.
- **`a-ok`** (assistant + non-empty content): the final reply of a turn. Show verbatim.
- **`[empty: DO NOT FABRICATE]`**: content empty AND no tool_calls AND no reasoning AND no api_content → genuinely compacted/lost. Do not invent.
- **`tool`** rows: `content` holds the tool result → `[tool-output]`.
- **`user`** rows: `content` is the real input.

## Tool-call parsing (both provider shapes)
`tool_calls` JSON varies by provider. Handle both so the function name resolves:
```python
def call_name(tc):
    out=[]
    arr = json.loads(tc)
    for t in arr if isinstance(arr, list) else [arr]:
        out.append(t.get('name') or (t.get('function') or {}).get('name') or t.get('tool_name') or '?')
    return ', '.join(out)
```

## Unit / tz reminders
- Hermes `messages.timestamp` = epoch **seconds**. Convert with `datetime(ts,'unixepoch','+5:30')` (IST).
- OpenCode `opencode.db` uses epoch **ms** — never merge the two without dividing ms->s; mismatched units silently return empty rows.
- `compacted` (boolean column) is **not** the same as `content==''`. A row can be un-compacted and still have empty content (a tool-call step). Use this recipe, not the column.
