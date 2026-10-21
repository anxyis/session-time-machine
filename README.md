# session-time-machine

**Read back any AI coding session you've ever had — across every harness on your machine.**

Your agents leave transcripts everywhere: Hermes, OpenCode, Cline, Codex, Antigravity.
Each one stores them differently, in a different format, in a different place. This skill
reads them all through **one command** and normalizes them into a single row shape, so a
transcript looks the same no matter which agent produced it.

```bash
python3 scripts/sessions.py all --list 20
python3 scripts/sessions.py hermes --peek 20261021_102432_2b9b6f21
python3 scripts/sessions.py codex --search "migration"
```

---

## Why

When a session goes sideways — the agent looped, the context blew up, you want to see what
it actually did before it died — you normally can't. Each harness hides its history behind
its own database format, its own naming, its own half-documented paths.

This tool makes every transcript queryable with the same verbs:

| Command | What it does |
|---|---|
| `--list [N]` | recent sessions across a harness |
| `--peek ID` | size, model, and **fidelity** before you load anything |
| `--transcript ID` | readable transcript (full text by default) |
| `--tail ID N` | the last N rows — the "what just happened" view |
| `--search TERM` | full-text search across sessions |
| `--jsonl ID` / `--json ID` / `--md ID` | machine-readable or markdown export |
| `--export ID --out F` | write to a file (`md`/`jsonl`/`json`/`txt`) |

Filters compose with AND semantics: `--role`, `--kind`, `--model`, `--provider`,
`--contains`, `--tool`, `--errors`, `--since/--until/--date`, `--clip N`.

The token-saving one:

```bash
# skip every tool call and tool output -- 80% smaller, same prose
python3 scripts/sessions.py hermes --jsonl <ID> --no-tools
```

## Fidelity is explicit

The tool never invents content. `content` is either the verbatim string or `null` — never
`""`, never a paraphrase.

- `--peek` reports `fidelity.complete: true|false` and lists every gap in
  `fidelity.unrecovered` (e.g. two `[response interrupted]` responses Hermes couldn't
  recover).
- Rows are tagged so you can tell what you're reading: `[ok]` real content, `[tool-call-step]`
  the agent invoked a tool, `[tool-output]` the result, `[thinking:N]` CoT (not the reply),
  `[empty: …]` genuinely unrecoverable, `[lifecycle]` harness bookkeeping.
- The 2.8M-character Hermes session that overflowed a small-context model is still fully
  recoverable through this tool — you just have to read it in pieces.

Read-only everywhere: every database is opened `mode=ro`. It never writes to your agents'
data.

## Install

### One command (no dependencies)

```bash
git clone https://github.com/anxyis/session-time-machine.git
cd session-time-machine
./install.sh
```

That installs into every agent it can detect in `$HOME`. Pick specific ones:

```bash
./install.sh --agent claude-code --agent codex   # global, these two
./install.sh --project --agent opencode           # into the current project instead
./install.sh --list                               # what is supported / detected
./install.sh --uninstall                          # remove
```

Supported: Claude Code, Codex, OpenCode, Cline, Gemini CLI, Antigravity CLI, Cursor, Amp,
GitHub Copilot, Hermes Agent, Aider Desk, Augment, Crush, Goose, Kilo, OpenClaw, Roo,
Windsurf, Zed, Warp.

### Or with the skills CLI

The repo follows the [Agent Skills](https://agentskills.io) layout, so the
[skills CLI](https://github.com/vercel-labs/skills) works too:

```bash
npx skills add anxyis/session-time-machine
```

### Manual

```bash
mkdir -p .claude/skills/session-time-machine
curl -o .claude/skills/session-time-machine/SKILL.md \
  https://raw.githubusercontent.com/anxyis/session-time-machine/main/skills/session-time-machine/SKILL.md
# plus scripts/ and references/ — the SKILL.md is only the instructions
```

Use the installer unless you specifically want a one-file copy.

## Requirements

Python 3.9+. Standard library only — no pip installs, no dependencies, no network access.
Reads whatever harnesses you already have installed; harnesses that aren't installed are
simply skipped.

## Harnesses

| Harness | Source | Notes |
|---|---|---|
| Hermes | `~/.hermes/state.db` | seconds-precision epochs, `api_content` recovery |
| OpenCode | `~/.local/share/opencode/opencode.db` | millisecond epochs, `part`-table search |
| Cline | `~/.cline/data/db/sessions.db` | transcript lives in an external JSON file |
| Codex | `~/.codex/sessions/*/*/rollout-*.jsonl` | Responses-API rollout format |
| Antigravity | `~/.gemini/antigravity-cli/brain/*/…/transcript_full.jsonl` | plaintext, lossless |

## Development

```bash
python3 skills/session-time-machine/scripts/test_sessions.py
```

85 checks, all against **live** databases — no mocks. The suite discovers a real session
per harness at runtime and asserts fidelity, filter, and export invariants against it.

## License

MIT — see [LICENSE](LICENSE).
