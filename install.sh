#!/usr/bin/env bash
# session-time-machine installer.
# Copies the skill into one or more coding agents' skills directories.
# Pure bash + coreutils; no dependencies, no network, no admin rights.
# Works on bash 3.2 (stock macOS) and bash 5.x (Linux).
set -eu

SKILL="session-time-machine"
REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO_ROOT/skills/$SKILL"

# agent_key | project_path | global_path
# Project paths are relative to $PWD; global paths to $HOME.
# Kept in sync with vercel-labs/skills docs/agents.md.
AGENTS=(
  "claude-code|.claude/skills|$HOME/.claude/skills"
  "codex|.codex/skills|$HOME/.codex/skills"
  "opencode|.opencode/skills|$HOME/.config/opencode/skills"
  "cline|.cline/skills|$HOME/.cline/skills"
  "gemini-cli|.agents/skills|$HOME/.gemini/skills"
  "antigravity-cli|.agents/skills|$HOME/.gemini/antigravity-cli/skills"
  "cursor|.agents/skills|$HOME/.cursor/skills"
  "amp|.agents/skills|$HOME/.config/agents/skills"
  "universal|.agents/skills|$HOME/.config/agents/skills"
  "github-copilot|.agents/skills|$HOME/.copilot/skills"
  "hermes-agent|.hermes/skills|$HOME/.hermes/skills"
  "aider-desk|.aider-desk/skills|$HOME/.aider-desk/skills"
  "augment|.augment/skills|$HOME/.augment/skills"
  "crush|.crush/skills|$HOME/.config/crush/skills"
  "goose|.goose/skills|$HOME/.config/goose/skills"
  "kilo|.agents/skills|$HOME/.kilo/skills"
  "openclaw|skills|$HOME/.openclaw/skills"
  "roo|.roo/skills|$HOME/.roo/skills"
  "windsurf|.windsurf/skills|$HOME/.codeium/windsurf/skills"
  "zed|.agents/skills|$HOME/.agents/skills"
  "warp|.agents/skills|$HOME/.warp/skills"
  # Pi (earendil-works/pi) and OMP (can1357/oh-my-pi) both load user-level
  # skills from ~/.agents/skills -- verified in the pi-coding-agent dist
  # (getHomeDir()/.agents/skills) and in the omp binary's own loader strings.
  "pi|.agents/skills|$HOME/.agents/skills"
  "omp|.agents/skills|$HOME/.agents/skills"
)

usage() {
  cat <<'EOF'
session-time-machine installer

Usage:
  ./install.sh                 install for every detected agent (global)
  ./install.sh --project       install into the current project instead of globally
  ./install.sh --agent NAME    install for one agent (repeatable)
  ./install.sh --all           install for every known agent, detected or not
  ./install.sh --list          list known agents and whether they are detected
  ./install.sh --dry-run       show what would happen, change nothing
  ./install.sh --uninstall     remove the skill from the selected targets
  ./install.sh --help

Examples:
  ./install.sh --agent claude-code --agent codex
  ./install.sh --project --agent opencode
EOF
}

TARGETS=""
PROJECT=0; ALL=0; LIST=0; UNINSTALL=0; DRYRUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT=1 ;;
    --global)  PROJECT=0 ;;
    --agent)
      if [ $# -lt 2 ]; then echo "error: --agent needs an agent name" >&2; exit 2; fi
      TARGETS="$TARGETS $2"; shift ;;
    --all)     ALL=1 ;;
    --list)    LIST=1 ;;
    --dry-run) DRYRUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

[ -d "$SRC" ] || { echo "skill source not found: $SRC" >&2; exit 1; }

# A global agent dir's *parent* is the marker that the agent is installed.
detect() {
  case "$1" in
    claude-code)     [ -d "$HOME/.claude" ] ;;
    codex)           [ -d "$HOME/.codex" ] ;;
    opencode)        [ -d "$HOME/.config/opencode" ] || [ -d "$HOME/.opencode" ] ;;
    cline)           [ -d "$HOME/.cline" ] ;;
    gemini-cli)      [ -d "$HOME/.gemini" ] ;;
    antigravity-cli) [ -d "$HOME/.gemini/antigravity-cli" ] ;;
    cursor)          [ -d "$HOME/.cursor" ] ;;
    amp|universal)   [ -d "$HOME/.config/agents" ] ;;
    github-copilot)  [ -d "$HOME/.copilot" ] ;;
    hermes-agent)    [ -d "$HOME/.hermes" ] ;;
    aider-desk)      [ -d "$HOME/.aider-desk" ] ;;
    augment)         [ -d "$HOME/.augment" ] ;;
    crush)           [ -d "$HOME/.config/crush" ] ;;
    goose)           [ -d "$HOME/.config/goose" ] ;;
    kilo)            [ -d "$HOME/.kilo" ] ;;
    openclaw)        [ -d "$HOME/.openclaw" ] ;;
    roo)             [ -d "$HOME/.roo" ] ;;
    windsurf)        [ -d "$HOME/.codeium" ] ;;
    zed)             [ -d "$HOME/.agents" ] ;;
    warp)            [ -d "$HOME/.warp" ] ;;
    pi)              [ -d "$HOME/.pi" ] ;;
    omp)             [ -d "$HOME/.omp" ] ;;
    *) return 1 ;;
  esac
}

# Warn if this agent's skills root ends up with two copies of the skill
# (the agent keys skills by name, so a duplicate is ambiguous).
_dup_check() {
  root="$1"
  [ -d "$root" ] || return 0
  n=0
  for d in "$root"/*; do
    [ -d "$d" ] || continue
    if [ -d "$d/$SKILL" ] && [ -f "$d/$SKILL/SKILL.md" ]; then
      n=$((n+1))
    fi
  done
  if [ -f "$root/$SKILL/SKILL.md" ]; then
    n=$((n+1))
  fi
  if [ "$n" -gt 1 ]; then
    echo "  WARNING: $root now holds $n copies of $SKILL." >&2
    echo "           Agents key skills by name -- remove the extras." >&2
  fi
}

known() {
  for a in "${AGENTS[@]}"; do echo "${a%%|*}"; done
}

entry_for() {
  for a in "${AGENTS[@]}"; do
    if [ "${a%%|*}" = "$1" ]; then echo "$a"; return 0; fi
  done
  return 1
}

if [ "$LIST" = 1 ]; then
  printf '%-18s %-10s %s\n' AGENT DETECTED TARGET
  for a in "${AGENTS[@]}"; do
    k="${a%%|*}"; rest="${a#*|}"; proj="${rest%%|*}"; glob="${rest##*|}"
    if detect "$k"; then d=yes; else d=no; fi
    if [ "$PROJECT" = 1 ]; then t="$proj"; else t="$glob"; fi
    printf '%-18s %-10s %s\n' "$k" "$d" "$t"
  done
  exit 0
fi

# Resolve the list of agents to act on.
if [ -n "$TARGETS" ]; then
  for t in $TARGETS; do
    if ! known | grep -qx "$t"; then
      echo "unknown agent: $t" >&2
      echo "known: $(known | tr '\n' ' ')" >&2
      exit 2
    fi
  done
elif [ "$ALL" = 1 ]; then
  TARGETS="$(known | tr '\n' ' ')"
else
  for a in "${AGENTS[@]}"; do
    k="${a%%|*}"
    if detect "$k"; then TARGETS="$TARGETS $k"; fi
  done
  if [ -z "$TARGETS" ]; then
    echo "No supported agents detected in \$HOME." >&2
    echo "Use --agent NAME to pick one, or --list to see what is supported." >&2
    exit 1
  fi
fi

# The payload is staged in a temp dir and moved into place, so an interrupted
# run never leaves a half-installed skill behind.
STAGE=""
cleanup() { if [ -n "$STAGE" ] && [ -d "$STAGE" ]; then rm -rf "$STAGE"; fi; }
trap cleanup EXIT INT TERM

installed=0
SEEN=""          # space-separated list of destinations already handled
for t in $TARGETS; do
  entry="$(entry_for "$t")"
  proj="${entry#*|}"; proj="${proj%%|*}"; glob="${entry##*|}"
  if [ "$PROJECT" = 1 ]; then base="$proj"; else base="$glob"; fi
  dest="$base/$SKILL"

  # Several agents can share one directory (pi/omp/zed all use .agents/skills).
  # Install once, then just note the alias.
  case " $SEEN " in
    *" $dest "*) echo "  skip    $dest (shared with another agent)"; continue ;;
  esac
  SEEN="$SEEN $dest"

  # GUARD: the destination must never be the source tree. Some agents use a
  # project path that collides with the repo layout (openclaw uses "skills/",
  # which IS the repo's own skills/ dir) -- installing there would delete the
  # source and then try to copy from the deleted source.
  src_abs="$(cd "$SRC" && pwd)"
  dest_parent="$(cd "$(dirname "$dest")" 2>/dev/null && pwd || echo "$PWD/$(dirname "$dest")")"
  dest_abs="$dest_parent/$(basename "$dest")"
  if [ "$dest_abs" = "$src_abs" ]; then
    echo "REFUSING: $dest is the skill source itself." >&2
    echo "  (openclaw's project path 'skills/' collides with the repo layout)" >&2
    echo "  use the global path instead:  ./install.sh --agent openclaw" >&2
    exit 1
  fi

  if [ "$DRYRUN" = 1 ]; then
    if [ "$UNINSTALL" = 1 ]; then
      echo "  would remove  $dest"
    else
      echo "  would install $dest"
    fi
    continue
  fi

  if [ "$UNINSTALL" = 1 ]; then
    if [ -d "$dest" ]; then
      rm -rf "$dest"
      echo "  removed  $dest"
      installed=$((installed+1))
    fi
    continue
  fi

  # stage -> move, so the skill dir is never half-written
  if [ -z "$STAGE" ]; then
    STAGE="$(mktemp -d 2>/dev/null || mktemp -d -t stm)"
  fi
  stage_skill="$STAGE/$SKILL"
  rm -rf "$stage_skill"
  mkdir -p "$stage_skill"
  cp -R "$SRC/scripts" "$stage_skill/scripts"
  if [ -d "$SRC/references" ]; then cp -R "$SRC/references" "$stage_skill/references"; fi
  cp "$SRC/SKILL.md" "$stage_skill/SKILL.md"
  find "$stage_skill" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$stage_skill" -name '*.pyc' -delete 2>/dev/null || true

  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  mv "$stage_skill" "$dest"
  echo "  installed $dest"
  _dup_check "$(dirname "$dest")"
  installed=$((installed+1))
done

if [ "$DRYRUN" = 1 ]; then
  echo "dry run -- nothing changed."
  exit 0
fi

if [ "$UNINSTALL" = 1 ]; then
  echo "removed $installed target(s)."
else
  echo "installed $installed target(s)."
  echo
  echo "Try it:"
  echo "  python3 $dest/scripts/sessions.py all --list 10"
fi
