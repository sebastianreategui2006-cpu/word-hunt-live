#!/bin/zsh
cd "${0:A:h}"
if /usr/bin/curl -fsS http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
  open http://127.0.0.1:8765
  exit 0
fi
VENV="/tmp/wordhunt-live-venv-$(id -u)"
if [[ ! -x "$VENV/bin/python" ]]; then
  PYTHON=""
  for candidate in python3.11 python3.12 python3.13 python3.14 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(not ((3, 11) <= sys.version_info[:2] < (3, 15)))' 2>/dev/null; then
      PYTHON="$candidate"
      break
    fi
  done
  if [[ -z "$PYTHON" ]]; then
    echo "Install Python 3.11 or newer from python.org, then double-click run.command again."
    read -k 1 '?Press any key to close...'
    exit 1
  fi
  "$PYTHON" -m venv "$VENV" || exit 1
fi
REQUIREMENTS_HASH="$(/usr/bin/shasum requirements.txt | /usr/bin/awk '{print $1}')"
if [[ ! -f "$VENV/.wordhunt-installed" ]] || [[ "$(<"$VENV/.wordhunt-installed")" != "$REQUIREMENTS_HASH" ]]; then
  "$VENV/bin/python" -m pip install -r requirements.txt || exit 1
  print -r -- "$REQUIREMENTS_HASH" > "$VENV/.wordhunt-installed"
fi
(for i in {1..120}; do
  if /usr/bin/curl -fsS http://127.0.0.1:8765/api/state >/dev/null 2>&1; then
    open http://127.0.0.1:8765
    break
  fi
  sleep 1
done) &
exec "$VENV/bin/python" app.py
