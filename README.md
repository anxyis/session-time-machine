<h1 align="center">◈ session-time-machine</h1>

<p align="center">
  <strong>read back any AI session you ever had — across every harness on your box</strong><br>
  <sub>Hermes · OpenCode · Cline · Codex · Antigravity</sub>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="MIT License" />
  <img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python 3.9+" />
  <img src="https://img.shields.io/badge/deps-none-5865F2" alt="No dependencies" />
  <img src="https://img.shields.io/badge/harnesses-5-ff69b4" alt="5 harnesses" />
  <img src="https://img.shields.io/badge/tests-85%2F85%20green-4CAF50" alt="85/85 tests" />
</p>

<br/>

```
 ╔════════════════════════════════════════════════════════════════════╗
 ║                                                                    ║
 ║    ▄██  ████  ▄██   ▄██  ███ ▄██▄ █  █                             ║
 ║   ██   █    ██   ██    █  █  █ ██ █                                ║
 ║    ███  ███   ███   ███   █  █  █ █ ██                             ║
 ║     ██ █      ██   ██  █  █  █ █  █                                ║
 ║   ▀██▀ ████ ▀██▀ ▀██▀ ███ ▀██▀ █  █                                ║
 ║                                                                    ║
 ║   █████ ███ █   █ ████       █   █  ▄█  ▄███ █  █ ███ █  █ ████    ║
 ║     █   █  ██ ██ █          ██ ██ █ █  █    █  █  █  ██ █ █        ║
 ║     █   █  █ █ █ ███        █ █ █ ███  █    ███   █  █ ██ ███      ║
 ║     █   █  █   █ █          █   █ █  █ █    █  █  █  █  █ █        ║
 ║     █  ███ █   █ ████       █   █ █  █ ▀███ █  █ ███ █  █ ████     ║
 ║   t i m e   m a c h i n e                                          ║
 ║                                                                    ║
 ╚════════════════════════════════════════════════════════════════════╝
```

<br/>

> [!TIP]
> **One-liner install** — clones, detects every coding agent on your machine, drops the skill in place:
> ```bash
> git clone https://github.com/anxyis/session-time-machine.git && cd session-time-machine && ./install.sh
> ```

<br/>

## ◈ the actual problem

Your agent left a transcript. It was 2.8 million characters long. Then the context window died,
Hermes couldn't compress it any further, and the session auto-reset — mid-thought, with the
agent's tool calls still half-finished.

Now you want to know **what it actually did before it died**. Except every harness on your
machine buries its history somewhere different:

| Harness | Where the bones are buried |
|---|---|
| **Hermes** | `~/.hermes/state.db` |
| **OpenCode** | `~/.local/share/opencode/opencode.db` |
| **Cline** | `~/.cline/data/db/sessions.db` + a JSON file it points at |
| **Codex** | `~/.codex/sessions/*/*/rollout-*.jsonl` |
| **Antigravity** | `~/.gemini/antigravity-cli/brain/*/…/transcript_full.jsonl` |

Five databases, five formats, five schemas. Each one half-documented at best. Digging
through them by hand is miserable.

`session-time-machine` reads **all of them** through one CLI and hands you back the same
shape every time.

```bash
python3 scripts/sessions.py all --list 20
python3 scripts/sessions.py hermes --peek 20261021_102432_2b9b6f21
python3 scripts/sessions.py codex --search "migration"
```

<br/>

## ◈ what you get

| Command | What it does |
|---|---|
| `--list [N]` | recent sessions for a harness |
| `--peek ID` | size, model, and **fidelity** *before* you load anything |
| `--transcript ID` | readable transcript — full text by default, no truncation |
| `--tail ID N` | last N rows. the "what just happened" view |
| `--preview ID` | short peek at the head of a session |
| `--search TERM` | full-text search across sessions |
| `--jsonl` / `--json` / `--md` | machine-readable or markdown |
| `--export ID --out F` | write it to a file (`md`/`jsonl`/`json`/`txt`) |

Filters stack with AND semantics — pick the ones you want:

```bash
--role user,assistant   --kind message,tool_call   --model <substr>
--provider <substr>     --contains <text>          --tool <name>
--errors                --since 7d  --until 3d  --date 2026-10-21
```

And the one that actually saves your tokens:

```bash
# skip every tool call and tool output — 80% smaller, same prose
python3 scripts/sessions.py hermes --jsonl <ID> --no-tools
```

<br/>

## ◈ it will not make stuff up

This matters more than it sounds. A blank row in a transcript is usually **not** data loss —
it's an assistant turn whose payload was a tool call, not prose. Naive dumps make it look
like the agent went mute, and then a fresh agent either **invents a reply** or **drops the
step** and breaks the whole chain.

So:

- `content` is the verbatim string or `null`. Never `""`, never a paraphrase, never a guess.
- Every row is tagged so you know what you're reading:
  `[ok]` real content · `[tool-call-step]` it called a tool · `[tool-output]` the result ·
  `[thinking:N]` CoT, **not** the reply · `[empty: …]` genuinely unrecoverable ·
  `[lifecycle]` harness bookkeeping
- `--peek` tells you upfront whether the session is **complete** and lists every gap.

```
fidelity: complete
fidelity: gaps reported
    - seq612: api_content present: '[response interrupted]'
    - seq613: api_content present: '[response interrupted]'
```

If a step can't be recovered, it says so. It never quietly drops it.

<br/>

## ◈ install

### the easy way

```bash
git clone https://github.com/anxyis/session-time-machine.git
cd session-time-machine
./install.sh
```

That finds every agent you've got in `$HOME` and copies the skill in. Pick and choose:

```bash
./install.sh --agent claude-code --agent codex   # global, these two
./install.sh --project --agent opencode           # into this project instead
./install.sh --list                               # what's supported / detected
./install.sh --uninstall                          # nuke it
```

Covers **21** agents: Claude Code, Codex, OpenCode, Cline, Gemini CLI, Antigravity CLI,
Cursor, Amp, GitHub Copilot, Hermes Agent, Aider Desk, Augment, Crush, Goose, Kilo,
OpenClaw, Roo, Windsurf, Zed, Warp, and the universal `.agents/skills/`.

### or via the skills CLI

The repo follows the [Agent Skills](https://agentskills.io) spec, so this works too:

```bash
npx skills add anxyis/session-time-machine
```

<br/>

## ◈ FAQ

#### Do I need to install anything?
No. Python 3.9+ and that's it. Standard library only — no pip, no npm, no network calls.
It reads whatever harnesses you already have; the ones you don't are simply skipped.

#### Is it safe? Will it touch my agent data?
Every database is opened `mode=ro`. It never writes to your agents' data, never moves
sessions around, never edits a transcript. It's a reader.

#### Why not just grep the JSON?
Because the interesting stuff — which row is a tool result, which is CoT, which is an
unrecoverable gap — is exactly the thing that's encoded differently in every format.
Grep can't tell a tool output from the model narrating it. This can.

#### Why Python and not Rust/Go/TypeScript?
Because it'd be a `brew install` and a compile step and a binary in your PATH for something
you run twice a month. Python's already there. Works on a Raspberry Pi, a container, a
laptop, whatever.

#### Does it work on Windows / macOS?
Paths are `~`-relative and it uses `pathlib` throughout, so yes. The test suite was
developed and verified on Linux.

<br/>

## ◈ what's in the works next? (o゜▽゜)o☆

- **A `--diff` mode** — point it at two sessions and see what actually changed between
  them. Right now you eyeball two transcripts and hope.
- **Streaming/live tail** — follow a session as it's happening instead of after the fact.
- **A `--summary` mode** that gives you the shape of a session (how many tool calls, what
  got read, what it edited) without reading the whole thing.
- **More harnesses** — Windsurf, Aider, Goose, and whatever else shows up with a weird
  enough storage format to be interesting.

<br/>

## ◈ development

```bash
python3 skills/session-time-machine/scripts/test_sessions.py
```

**85 checks, all against live databases** — no mocks, no fixtures. The suite discovers a
real session per harness at runtime and asserts fidelity, filter, and export invariants
against actual data. If your harness changes shape, the suite notices.

The Agy adapter is the fiddly one and it's worth explaining why: Antigravity's transcript
has a **closed enum of 16 step types** (`RUN_COMMAND`, `VIEW_FILE`, `CODE_ACTION`,
`GREP_SEARCH`, …) and a `source` field (`MODEL` / `SYSTEM` / `USER_EXPLICIT`). Classify by
that enum and everything lines up — a tool result becomes a tool result, paired to the
call that asked for it. Classify by content-sniffing and you get 2046 tool results
misfiled as the model talking to itself, which is exactly what happened before I fixed it.

<br/>

## ◈ license

MIT. See [LICENSE](LICENSE).

<br/>

## ◈ credits

Built on the [Agent Skills](https://agentskills.io) spec and the
[skills CLI](https://github.com/vercel-labs/skills) agent list, so the installer lands in
the same place every other skill does.

Antigravity's transcript format was reverse-engineered from the local Antigravity CLI
install and cross-checked against three independent open-source parsers.
