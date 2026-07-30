#!/usr/bin/env bash
# One-shot setup and launch.
#
#   ./run.sh              set up if needed, then start the web UI
#   ./run.sh demo         set up, load the sample statements, then start
#   ./run.sh test         run the test suite
#
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
PIP=.venv/bin/pip

if [ ! -x "$PY" ]; then
  echo "==> Creating virtual environment"
  python3 -m venv .venv
  "$PIP" install --quiet --upgrade pip
  echo "==> Installing dependencies"
  "$PIP" install --quiet -r requirements.txt
fi

case "${1:-serve}" in
  test)
    exec "$PY" -m pytest
    ;;
  demo)
    echo "==> Generating fictional sample statements"
    "$PY" samples/generate_samples.py
    echo "==> Importing them"
    "$PY" -m spendtracker.cli import-statement 'samples/*.csv'
    echo
    "$PY" -m spendtracker.cli status
    echo
    exec "$PY" -m spendtracker.cli serve
    ;;
  serve)
    exec "$PY" -m spendtracker.cli serve
    ;;
  *)
    exec "$PY" -m spendtracker.cli "$@"
    ;;
esac
