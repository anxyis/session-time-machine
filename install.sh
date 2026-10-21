#!/usr/bin/env bash
# session-time-machine installer.
# Copies the skill into one or more coding agents' skills directories.
# Pure bash + coreutils; no dependencies, no network, no admin rights.
set -euo pipefail

SKILL="session-time-machine"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
  ./install.sh --uninstall     remove the skill from the selected targets
  ./install.sh --help

Examples:
  ./install.sh --agent claude-code --agent codex
  ./install.sh --project --agent opencode
EOF
}

TARGETS=(); PROJECT=0; ALL=0; LIST=0; UNINSTALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT=1 ;;
    --global)  PROJECT=0 ;;
    --agent)   TARGETS+=("$2"); shift ;;
    --all)     ALL=1 ;;
    --list)    LIST=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
  shift
done

[ -d "$SRC" ] || { echo "skill source not found: $SRC" >&2; exit 1; }

# A global agent dir's *parent* is the marker that the agent is installed.
# e.g. ~/.claude -> claude-code, ~/.config/opencode -> opencode.
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
    *) return 1 ;;
  esac
}

known() {
  for a in "${AGENTS[@]}"; do echo "${a%%|*}"; done
}

if [ "$LIST" = 1 ]; then
  printf '%-18s %-10s %s\n' AGENT DETECTED TARGET
  for a in "${AGENTS[@]}"; do
    k="${a%%|*}"; rest="${a#*|}"; proj="${rest%%|*}"; glob="${rest##*|}"
    if detect "$k"; then d=yes; else d=no; fi
    printf '%-18s %-10s %s\n' "$k" "$d" "$([ "$PROJECT" = 1 ] && echo "$proj" || echo "$glob")"
  done
  exit 0
fi

# Resolve the list of agents to act on.
if [ ${#TARGETS[@]} -gt 0 ]; then
  for t in "${TARGETS[@]}"; do
    if ! known | grep -qx "$t"; then
      echo "unknown agent: $t" >&2
      echo "known: $(known | tr '\n' ' ')" >&2
      exit 2
    fi
  done
elif [ "$ALL" = 1 ]; then
  TARGETS=($(known))
else
  for a in "${AGENTS[@]}"; do
    k="${a%%|*}"
    if detect "$k"; then TARGETS+=("$k"); fi
  done
  if [ ${#TARGETS[@]} -eq 0 ]; then
    echo "No supported agents detected in \$HOME." >&2
    echo "Use --agent NAME to pick one, or --list to see what is supported." >&2
    exit 1
  fi
fi

installed=0
for t in "${TARGETS[@]}"; do
  entry="$(for a in "${AGENTS[@]}"; do [ "${a%%|*}" = "$t" ] && { echo "$a"; break; }; done)"
  proj="${entry#*|}"; proj="${proj%%|*}"; glob="${entry##*|}"
  base="$([ "$PROJECT" = 1 ] && echo "$proj" || echo "$glob")"
  dest="$base/$SKILL"

  if [ "$UNINSTALL" = 1 ]; then
    if [ -d "$dest" ]; then
      rm -rf "$dest"
      echo "removed  $dest"
      installed=$((installed+1))
    fi
    continue
  fi

  mkdir -p "$dest"
  # replace the payload, keep anything else already there
  rm -rf "$dest/scripts" "$dest/references"
  cp -R "$SRC/scripts" "$dest/scripts"
  [ -d "$SRC/references" ] && cp -R "$SRC/references" "$dest/references"
  cp "$SRC/SKILL.md" "$dest/SKILL.md"
  # strip any .pyc noise
  find "$dest" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  find "$dest" -name '*.pyc' -delete 2>/dev/null || true
  echo "installed $dest"
  installed=$((installed+1))
done

if [ "$UNINSTALL" = 1 ]; then
  echo "removed $installed target(s)."
else
  echo "installed $installed target(s)."
  echo
  echo "Try it:"
  echo "  python3 $dest/scripts/sessions.py all --list 10"
fi
