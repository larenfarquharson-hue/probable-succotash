#!/usr/bin/env bash
# One-shot setup and launch.
#
#   ./run.sh              set up if needed, then start the web UI
#   ./run.sh demo         set up, load the sample statements, then start
#   ./run.sh test         run the test suite
#   ./run.sh <command>    any spendtracker command, e.g. ./run.sh report
#
# This script builds a virtualenv with the optional extras, because the web UI
# needs Flask. The CLI itself needs nothing: `python3 -m spendtracker.cli` works
# straight from a clone with no virtualenv and no installs at all.
#
set -euo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
PIP=.venv/bin/pip

if [ ! -x "$PY" ]; then
  echo "==> Creating virtual environment"
  python3 -m venv .venv
  "$PIP" install --quiet --upgrade pip
  echo "==> Installing optional extras (the CLI needs none of these)"
  "$PIP" install --quiet -r requirements.txt
fi

case "${1:-serve}" in
  test)
    exec "$PY" -m pytest
    ;;
  demo)
    echo "==> Generating fictional sample statements"
    "$PY" samples/generate_samples.py
    echo "==> Inspecting one before importing anything"
    "$PY" -m spendtracker.cli inspect samples/statement_march.csv
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
