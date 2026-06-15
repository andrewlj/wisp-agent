#!/bin/sh
# wisp — launcher. Run the agent CLI with the right interpreter from anywhere.
#   wisp                      # interactive REPL
#   wisp gateway status       # manage the Telegram gateway
#   wisp schedule list        # manage scheduled jobs
#   wisp run "<task>"         # one-shot headless task
# Resolve symlinks so the launcher works when symlinked onto PATH.
SELF="$0"
while [ -L "$SELF" ]; do
  link="$(readlink "$SELF")"
  case "$link" in
    /*) SELF="$link" ;;
    *)  SELF="$(dirname "$SELF")/$link" ;;
  esac
done
DIR="$(cd "$(dirname "$SELF")" && pwd -P)"
if command -v python3.11 >/dev/null 2>&1; then PY=python3.11; else PY=python3; fi
exec "$PY" "$DIR/agent.py" "$@"
