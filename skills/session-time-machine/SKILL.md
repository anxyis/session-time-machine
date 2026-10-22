---
name: session-time-machine
description: Use when digging through past agent sessions and conversation transcripts across harnesses (Hermes, OpenCode, Cline, Codex, Antigravity, Omp, Pi).
---

# Session Time Machine

A single, deterministic CLI (`scripts/sessions.py`) that inspects, searches, filters, and exports **complete** AI conversations from every installed harness. It normalizes each harness into one row shape, then runs every command through a shared filter + printer layer, so behavior is identical whether you're reading Hermes or Antigravity. Read-only everywhere (every DB is opened `mode=ro`).

## Quick start
`S` is the skill's own script — resolve it relative to this `SKILL.md` (the path
differs per agent: `~/.claude/skills/session-time-machine/`, `.agents/skills/…`, etc.).
```bash
S=./scripts/sessions.py          # from the skill directory
python3 $S hermes --list 20
python3 $S hermes --peek <ID>                 # size + fidelity before loading
python3 $S hermes --jsonl <ID> | head -40     # chunkable, content=null where lost
python3 $S hermes --md <ID>                   # full, for read_file(offset=N)
```
Tests: `python3 scripts/test_sessions.py` → **116/116 green** (live) · **91/91** (fixture corpus, `tests/run_fixtures.sh`), all against live databases (no mocks). Re-run after any harness change.

## Normalized row (one shape for every harness)
```
{role, seq, ts, epoch, kind, tag, content|null, reasoning_chars, tool_names,
 tool_call_id, model, provider, is_error, attachments, unrecovered[], ...}
```
`kind` ∈ `message | tool_call | tool_output | reasoning | empty | lifecycle | error | attachment`.
`content` is **always** the verbatim string or `null` (never `""`, never invented).

`role` follows the harness's own convention, consistently across all five: `user` = the human, `assistant` = the model's own prose/reasoning/tool_call, `developer` = injected system context, `system` = lifecycle/error rows, **`tool` = every `tool_output` row**. A tool result is never `role=assistant` — that mislabel makes a transcript read as if the model had narrated the tool output.

### The one hallu­ci­nation trap the tagger prevents
An assistant turn that invoked tools has `content==null` (its payload is the tool call, not prose). The tagger labels it instead of leaving a blank `[assistant]` line:
- `[ok]` — real message content.
- `[tool-call-step]` — assistant invoked tools; prints the real function name parsed from `tool_calls` (OpenAI+Anthropic shapes).
- `[tool-output]` — tool result.
- `[thinking:N — CoT, not the reply]` — reasoning/CoT is labeled and must not be read as the reply.
- `[empty: … — DO NOT FABRICATE]` — content/tool_calls/reasoning/api_content all absent: genuinely unrecoverable, `content=null`.
- `[lifecycle]` — harness bookkeeping (Codex `world_state`/`turn_context`, Agy run steps); hidden by default, shown with `--include-lifecycle`.
- **context state** (Hermes) — every row carries one of three states, in the `tags` field of `--json`/`--jsonl`:
  - `live` — the row is in the model's live context right now.
  - `compacted` — the row was **superseded by a context compaction**. Its content is still in the database, but the model no longer sees it; a summary replaced it.
  - `retained` — older row, kept, not compacted, just outside the current window.

  This matters when reading a long session: a compacted session is **not** a straight-line conversation of what the model had in context. `--context live` shows only what the model currently sees; `--context compacted` shows only what it no longer sees. `--peek` prints the split.

## The search index (`--reindex`)
`--search` used to be a linear scan of every session file in every harness. On this box that is 264,738 rows across the sqlite stores plus 5.8 MB of JSONL, and a search took **8.8 seconds**.

`--reindex` builds a local FTS5 index at `~/.cache/session-time-machine/index.db` (override with `STM_INDEX_DIR`). The same search then takes **0.55 seconds** — 16x faster.

The rules that keep it honest, each one added because a test caught the opposite:
- **The index is a cache, never a source of truth.** It stores the *redacted* text, so it cannot become a plaintext copy of your transcripts. Deleting it loses nothing; the search falls back to the scan.
- **An incomplete index is never used.** `--index-limit` (or `STM_INDEX_LIMIT`) builds a partial index, and a partial index would silently *lose* results — so it is marked incomplete and searches use the scan instead. `--index-status` says which you have.
- **It is tied to the corpus it was built from.** The harness data roots are fingerprinted into the index; if they change (a fixture directory, a different db), the index does not describe what you are searching, so it is not used.
- **The rebuild is incremental.** Each row carries a fingerprint; only changed rows are re-indexed. A second run over an unchanged corpus re-indexes almost nothing. Agy sessions are the exception — their transcripts are live-appended, so their rows do change.
- **FTS5 tokenizes, the scan does substring matching.** So the FTS5 match is only a *candidate* set; the authoritative substring check runs inside the index. The index can make a search faster, never change what it finds.

`STM_NO_INDEX=1` forces the linear scan, which is what you want when you want the old behaviour or when the index is stale.

## Inspecting without reading (`--summary`, `--diff`)
`--summary ID` prints the shape of a session: row count, content and reasoning size, a role/kind breakdown with percentages, the tool-usage histogram, the fidelity verdict, and the opening prompt plus the closing answer. It is computed from row metadata, so it never puts the transcript in your context. On the 2,898-row sample session the summary is ~1.5 KB against a 2.8M-char transcript.

`--diff ID_A ID_B` compares two sessions of the same harness:
- **STRUCTURE** — rows, kind breakdown, content chars, with deltas.
- **TOOL USAGE** — every tool whose call count differs.
- **MODIFIED TOOL CALLS** — the tools called in *both* sessions but with *different arguments*. The argument hash ignores the volatile call ids, so the same call with a new id is not a modification. (Grouping by tool, not by arghash: two different sessions almost never share an arghash, so listing those said nothing.)
- **PROMPT DRIFT** — the human's own messages compared: whether the opening intent changed, and the first message where they diverged. Harness-injected rows are skipped, as in the handoff pack.

## The handoff pack (`--handoff`)
When a session dies and you need to start a new one that knows what you were doing, `--handoff` builds one block of text to paste into the fresh session:

```
# HANDOFF - hermes session <id>
- source / session size / context split / tail token count
## LAST INTENT     the human's own last request
## IN PROGRESS     tool call batches that never got a result
## TAIL            the last N rows, token-budgeted, tail-preserving
```

Two things it gets right that a naive tail would not:
- **The last intent is not the last user message.** In a compacted session the last user rows are Hermes' own `[CONTEXT COMPACTION ...]` notices. Seeding a new session with one of those is useless, so injected rows are skipped — but if every user row is injected, the pack still reports the last one and says so.
- **Unfinished tool calls are found by batch, not by row.** One assistant message with N tool calls becomes N consecutive `tool_call` rows sharing a call id, and their results come *after* the run of calls. A batch is finished if tool_output rows follow it before the next message.

The tail is token-budgeted (`--handoff-budget`, default 4000) and keeps the **end** — the question is "what happened before it died". The whole pack goes through the same redaction and the same untrusted-content fence as `--transcript`, because it is a transcript excerpt.

## Commands (all harnesses)
```
harness = hermes|opencode|cline|codex|agy|all
python3 $S <harness> --list [N]
python3 $S <harness> --peek ID
python3 $S <harness> --transcript ID [N] | --tail ID N | --preview ID [N]
python3 $S <harness> --md ID | --jsonl ID | --json ID
python3 $S <harness> --export ID --out FILE --format md|jsonl|json|txt
python3 $S <harness> --search TERM
python3 $S <harness> --logs N          # agy run logs only
```
`all --list` and `all --search TERM` fan out across Hermes/OpenCode/Cline/Codex/Agy. Unsupported combos exit non-zero with a message instead of silent no-ops.

## Composable filters (AND semantics; applied before printing)
Filters SELECT rows only — they never mutate a row, so a surviving row is identical with or without filters. Active filters are reported on stderr; stdout stays machine-parseable.
```
--no-tools            drop tool_call/tool_output rows
--reindex             update the local FTS5 search index (incremental)
--reindex-force       rebuild the search index from scratch
--index-status        what is in the search index
--index-limit N       index at most N sessions per harness
--summary ID          session shape, token footprint, tool usage --
                      without reading the transcript
--diff ID_A ID_B      compare two sessions: structure, modified tool
                      calls, prompt drift
--handoff ID          build a handoff pack: last intent, unfinished
                      tool calls, and a token-budgeted tail, ready to
                      seed a fresh session
--handoff-budget N    token budget for the handoff tail (default 4000)
--handoff-tail N      max rows considered for the tail (default 40)
--context live       hermes: only the rows in the model's live context
--context compacted  hermes: only the rows compacted away (content kept)
--include-lifecycle   un-hide [lifecycle] rows (default: hidden)
--role user,assistant,...      keep roles
--kind message,tool_call,...   keep kinds
--model <substr>    --provider <substr>   substring match on the row's model/provider
--contains <text>    case-insensitive substring over content + reasoning
--tool <name>        keep tool_call/tool_output rows that invoked <name>
--errors             keep only is_error rows
--since DAYS --until DAYS --date YYYY-MM-DD   epoch-based, IST (host) time
--clip N             truncate each text row to N chars with an explicit "… [+M clipped]" marker (default: no clipping — full fidelity)
```
Date filters use `epoch`, normalized per harness (Hermes seconds, OpenCode milliseconds, Codex/session_meta timestamps, Agy step idx→file mtime). `--no-tools` and `--include-lifecycle` are honored on **every** output path (transcript/tail/preview/md/jsonl/json/export). `--peek` always reports the full counts (it is a summary, not a transcript).

## Data roots (env-overridable)
Each harness's data root can be overridden, which is what lets the test suite run
against a fixture corpus instead of live data:
`STM_HERMES_DB`, `STM_OPENCODE_DB`, `STM_CLINE_DB`, `STM_CODEX_DIR`, `STM_AGY_BRAIN`,
`STM_AGY_DIR`, `STM_OMP_SESSIONS`, `STM_PI_SESSIONS`. See `tests/run_fixtures.sh`.

## Harness coverage (verified 2026-10-22)
| Capability | Hermes | OpenCode | Cline | Codex | Agy | Omp | Pi |
|---|---|---|---|---|---|---|---|
| list / peek | ✅ | ✅ | ✅ | ✅ | ✅ (+ `--logs`) | ✅ | ✅ |
| complete transcript | ✅ | ✅ | ✅ | ✅ | ✅ (plaintext `transcript_full.jsonl`) | ✅ (event-sourced jsonl) | ✅ (same format) |
| `--no-tools`, `--role`, `--model`, `--errors` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| search | ✅ (content+tool_calls) | ✅ (`part.text`) | ✅ | ✅ (rollout text) | ✅ (plaintext transcript) | ✅ (raw line scan) | ✅ (same) |
| export md/jsonl/json/txt | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| fidelity report | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

- **Hermes** — `~/.hermes/state.db` (epoch **seconds**). `api_content` recovered for interrupted responses (tagged `[empty: … response interrupted …]`); never fabricated.
- **OpenCode** — `~/.local/share/opencode/opencode.db` (epoch **ms**). Reimplemented as a normalized row stream (was text-only `--tail`); `--search` now queries the `part` table (`json_extract(data,'$.text')`), which the prior version searched `message.data` (metadata-only → always empty). Model parsed from `session.model` JSON.
- **Cline** — `~/.cline/data/db/sessions.db`; each session's transcript lives in an external JSON file referenced by `messages_path`. Three recoveries: (1) `tool_result.content` (a JSON **list** of blocks) is flattened by a recursive block extractor — a prior `_block_texts` called itself on a single block dict (it only handled lists) and silently dropped every structured result; (2) tool results — Cline stores them as `role:"user"` `tool_result` blocks — are emitted as `tool_output` rows paired to their call via `tool_use_id` (previously misfiled as empty user messages); (3) per-turn `thinking` blocks are emitted as `reasoning` rows (a `think` char-count was computed then discarded) and `tool_use.input` is carried on the `tool_call` row so argument values survive.
- **Codex** — `~/.codex/sessions/*/*/rollout-*.jsonl` (Responses-API). `world_state`/`turn_context` emitted as `[lifecycle]` (hidden by default) — no longer leak as fake `[ok]` assistant messages. Session id taken from the rollout filename.
| **Agy** — Antigravity CLI keeps a plaintext, structured `brain/<convId>/.system_generated/logs/transcript_full.jsonl` (one JSON step per line: `type`/`source`/`status`, `content`, `tool_calls[{name,args}]`, `thinking`, RFC3339 `created_at`). This is the **primary** source and yields `fidelity.complete:true` (lossless: tool calls/args, thinking, status, timestamps all recovered). The per-conversation SQLite `gen_metadata` protobuf is decoded **only for metadata** — field 1→field 19 = `model_id`, and usage fields 1→4→{2 uncached-input, 3 output-incl-thinking, 5 cache-read} — reported as a session-level `session.usage` dict (these totals are not summed into the transcript rows and are clearly labeled). Per-generation blobs that fail the plausibility gate (missing input/output fields) are counted out, never summed, and surfaced as `session.usage.gen_skipped` so nothing is silently lost. The DB `step_payload` protobuf is a **fallback** only when a brain transcript is absent (then `fidelity.complete:false`), disclosed as protobuf-string-extracted, never schema-decoded.
- **Omp / Pi** — [Pi](https://github.com/earendil-works/pi) (badlogic/pi-mono) and [OMP](https://github.com/can1357/oh-my-pi) (a fork of Pi) share a session format: `~/.omp/agent/sessions/<project>/<ISOts>_<uuid>.jsonl` (OMP) and `~/.pi/agent/sessions/<project>/<ISOts>_<uuid>.jsonl` (Pi). The file is **event-sourced** — every line is an event with `id`/`parentId`. `message` events carry `role=user|assistant|toolResult` and a `content` list of parts (`text`/`thinking`/`toolCall`/`image`). A tool result is a *separate* `role=toolResult` message carrying the same `toolCallId` as the call, so calls and results pair exactly. `custom`/`custom_message` events (tool_execution_start, async-result, session_exit) become lifecycle/developer rows. `model_change` → model; `model_usage` → session-level usage. The `session` header holds the authoritative start time (the `title` event's `updatedAt` is the *rename* time, not the start). A tool call with no result is reported in `fidelity.unrecovered`.

## What "complete" means here
`--md` / `--jsonl` / `--json` are lossless (full bodies). `--transcript` / `--tail` / `--preview` are readable previews; use `--clip N` to make truncation explicit (otherwise transcript bodies are full). A session is flagged `fidelity.complete:false` whenever anything could not be recovered. **Precisely:** `complete` is *transcript-parsing completeness* — true iff every source record parsed and mapped to a row with no unrecoverable gap (each gap listed in `session.fidelity.unrecovered`, e.g. the two `[response interrupted]` Hermes rows, or Agy's protobuf-fallback bytes). Two things are **always separate** from `complete`: (1) session-level `session.usage` token totals (decoded from Agy `gen_metadata`, never summed into transcript rows); (2) the protobuf DB path, used only for metadata and itself flagged `complete:false` as a fallback. `content` is `null`, never invented.

## Output safety
Three guards, all on by default, all overridable:

- **Redaction** (`--no-redact` to disable) — secret-shaped substrings are scrubbed on
  the way OUT: GitHub/OpenAI/Anthropic/Google/AWS/Slack tokens, JWTs, bearer headers,
  `password=`/`api_key=` assignments, URL credentials, and secret-looking env vars.
  Placeholders (`your_key_here`, `AKIAIOSFODNN7EXAMPLE`, `${VAR}`) are left alone.
  Redaction never touches the parsed row — `content` stays verbatim in memory (that is
  the fidelity contract) — and what was scrubbed is always reported on stderr.
- **Fencing** (`--no-fence` to disable) — replayed transcripts are wrapped in an
  explicit untrusted-data banner. Tool output can contain text crafted to look like
  instructions; the fence tells the reader it is data, not commands.
- **Context budget** (`--budget N`, default 120000 chars; `--budget 0` = unlimited) —
  `--transcript`/`--md`/`--preview`/`--tail` cap the printed body and state exactly how
  much was left out. `--json`/`--jsonl`/`--export` stay lossless.

The budget exists because the tool's own failure mode is the one it must not cause:
a 2.8M-char dump into an agent's context is what killed the 2026-10-21 session.

## Gotchas
- **Never paste a whole `--json` dump into your context.** A single Hermes session is ~3.5 MB of JSON; two or three of those will overflow a small-context model and kill the session (this is exactly how the 2026-10-21 session died: context overflow on `kc/poolside/laguna-s-2.1:free`, auto-reset). Aggregate in Python instead — `execute_code` with `json.loads` + `Counter`, print only the summary. The same rule applies to any transcript: `--peek` first, then `--jsonl | head -40`, then `--md` + `read_file(offset=N)` for the part you actually need.
- **Time units differ:** Hermes `messages.timestamp` = epoch **seconds** (float); OpenCode = **milliseconds**. Filters derive `epoch` per-harness — never hand-merge without normalizing.
- **tz:** host is UTC; `ts` is rendered IST (`+5:30`) per `config.timezone` + `gateway.message_timestamps`. All `--since/--until/--date` filters compare on the normalized `epoch`, so tz can't drift the filter.
| **Agy schema:** the plaintext transcript's `type` field is a **closed enum** (SpecStory §3.2) — 16 values observed live: `USER_INPUT`, `PLANNER_RESPONSE`, `GENERIC`, `SYSTEM_MESSAGE`, `CHECKPOINT`, `CONVERSATION_HISTORY`, `ERROR_MESSAGE`, `EPHEMERAL_MESSAGE`, `RUN_COMMAND`, `VIEW_FILE`, `CODE_ACTION`, `GREP_SEARCH`, `LIST_DIRECTORY`, `FIND`, `INVOKE_SUBAGENT`, `MCP_TOOL`. Rows are classified from `type` + `source` (never from content sniffing): `source=USER_EXPLICIT`→user; `source=MODEL` + `PLANNER_RESPONSE`→assistant (thinking/content/tool_calls); `source=MODEL` + any other type→**tool_output**; `SYSTEM_MESSAGE`/`EPHEMERAL_MESSAGE`→developer; `CHECKPOINT`/`CONVERSATION_HISTORY`→lifecycle; `ERROR_MESSAGE`→error (`is_error`). A result is paired to its call via the type→tool map (`RUN_COMMAND`→`run_command`, `VIEW_FILE`→`view_file`, `CODE_ACTION`→`write_to_file`/`replace_file_content`/`multi_replace_file_content`, `LIST_DIRECTORY`→`list_dir`, `FIND`→`find_by_name`, `MCP_TOOL`→`call_mcp_tool`, …); `GENERIC` has no dedicated type and pairs to the oldest unmatched call. An unknown `type` is kept as a lifecycle row **and** listed in `fidelity.unrecovered` — never guessed. A brain transcript is required for full fidelity; sessions without one fall back to protobuf string-extraction (`complete:false`). Status `INVALID` (observed in the wild, not in SpecStory's docs) is treated as `is_error`.
- **Multimodal:** attachments are recorded (model/path) but their bytes are not decoded; they appear as `[attachment]` rows with metadata, not rendered pixels.
- **A bad session id exits 2, it does not print an empty session.** `_resolve_session` used to fall through and return the raw string when nothing matched, so `stm peek typo` emitted a phantom zero-message session and exited 0 — indistinguishable from a real-but-empty session to a caller. Any id-taking verb (`peek`/`summary`/`handoff`/`tail`/`transcript`/`preview`/`md`/`jsonl`/`json`/`export`/`path`/`diff`) now writes `error: no session matching '<id>'` to stderr and exits 2. Prefix matching is intentional: a 3-char id that matches two sessions exits 2 as *ambiguous*, so scripts should pass ≥8 chars or a full id. Scope matters too — `stm codex peek <hermes-id>` is a miss (exit 2), not a cross-harness fallback.
- **`last` respects `--source`, so it agrees with `ls`.** `last` resolves to the newest session *within the current source filter*, not the newest overall. Default (`--source` unset) hides `cron`, so `stm peek last` will not hand you a cron run; use `--source cron` for those or `--source all` for everything regardless. This is a fix for the earlier divergence where `--source cli last` could return an excluded cron session.

## Testing
`scripts/test_sessions.py` is data-driven: it discovers a real session per harness at runtime (including a tool-bearing one) and asserts, against the **live** databases:
- JSONL/JSON parse with no loss; `len==sum(by_kind)`; every `content` is a string or `null` (never `""`); kinds are all known.
- `--no-tools` removes exactly the `tool_call`/`tool_output` kinds and preserves every other row.
- `--role`/`--model` filters are real selections (keep matches, drop the rest).
- `--search TERM` (term derived from the session's own first user message) returns that session.
- Hermes: the `compacted`/`active` flags are surfaced per row, and the peek/fidelity report shows the live / compacted-away / retained split. A long session is typically ~10% live, ~65% compacted-away, ~25% retained — so a transcript presented without this split overstates what the model actually had in context.
- Codex: no `world_state`/`turn_context` leak by default; `--include-lifecycle` surfaces them; tool envelope present; list timestamps are dates, not raw keys.
| Agy: `fidelity.complete:true` via `transcript_full.jsonl`; model recovered from `gen_metadata` (field 1→19 `model_id`) when `settings.json` omits it; `session.usage` token fields decode to sane per-generation values (`output << input`, e.g. 31.6M uncached-input / 271M cache-read for a 4444-step session); `--search` returns the session from the plaintext transcript; `counts()` buckets sum exactly to `msgs` (no double-counting of `reasoning`/`tool_output` into `a_ok`; developer rows get a `d:` bucket).
| Omp/Pi: the session jsonl is event-sourced — every line is an event with `id`/`parentId`. `message` events carry `role=user|assistant|toolResult`; content parts are `text`/`thinking`/`toolCall`/`image`. A tool result is a **separate `role=toolResult` message** carrying the same `toolCallId` as the call, so calls and results pair exactly (`tool_call` count == `tool_output` count, verified 188/188 on the live session). `custom`/`custom_message` events (tool_execution_start, async-result, …) become lifecycle/developer rows. `model_change` → model; `model_usage` → session-level usage (input/output/cacheRead/cacheWrite/cost), never summed into rows. The `session` header carries the authoritative start time — the `title` event's `updatedAt` is the *rename* time, so the title is stamped with the session start. A tool call with no result is reported in `fidelity.unrecovered`.
- Ordering (epoch non-decreasing), `--clip` marker, `--export` jsonl==json message count, `all --list`/`all --search` exit 0.

Run: `python3 scripts/test_sessions.py` (live data) or `bash tests/run_fixtures.sh`
(sanitized fixture corpus — portable, and what CI runs).

## Fixture corpus
`tests/fixtures/` holds one synthetic session per harness, built to the real schema
(hermes/opencode/cline DBs, a codex rollout with the real `custom_tool_call` shape, an
agy brain transcript + `gen_metadata` protobuf, and the shared omp/pi event jsonl). The
fixture suite (109 checks) needs no harness installed and no personal data. The live
suite (116 checks) asserts the same invariants against real sessions. CI runs the
fixture suite, the installer self-test, the bash-3.2 check, and a secret scan.

`tests/captures/` holds SANITIZED REAL captures -- actual sessions, secrets
redacted, home paths and IPs masked, content trimmed, and the harness version
recorded in `VERSIONS.json`. Synthetic fixtures only pin the author's *reading*
of each format; the captures pin the format itself. CI scans them for leaks on
every push (verified against a planted one).
