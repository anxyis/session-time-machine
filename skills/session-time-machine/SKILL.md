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
Tests: `python3 scripts/test_sessions.py` → **98/98 green** (live) · **91/91** (fixture corpus, `tests/run_fixtures.sh`), all against live databases (no mocks). Re-run after any harness change.

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

## Testing
`scripts/test_sessions.py` is data-driven: it discovers a real session per harness at runtime (including a tool-bearing one) and asserts, against the **live** databases:
- JSONL/JSON parse with no loss; `len==sum(by_kind)`; every `content` is a string or `null` (never `""`); kinds are all known.
- `--no-tools` removes exactly the `tool_call`/`tool_output` kinds and preserves every other row.
- `--role`/`--model` filters are real selections (keep matches, drop the rest).
- `--search TERM` (term derived from the session's own first user message) returns that session.
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
fixture suite (92 checks) needs no harness installed and no personal data. The live
suite (110 checks) asserts the same invariants against real sessions. CI runs the
fixture suite, the installer self-test, the bash-3.2 check, and a secret scan.

`tests/captures/` holds SANITIZED REAL captures -- actual sessions, secrets
redacted, home paths and IPs masked, content trimmed, and the harness version
recorded in `VERSIONS.json`. Synthetic fixtures only pin the author's *reading*
of each format; the captures pin the format itself. CI scans them for leaks on
every push (verified against a planted one).
