"""The core CLI must work with nothing installed but Python.

This is a promise the README makes, and it is the kind of promise that decays
silently: one convenience import of a third-party package at module scope and
the app stops installing on a locked-down machine, with nothing failing until
someone tries it. So assert it.

Every optional package is made unimportable, then the core commands are run in
a subprocess. `serve` is expected to fail — but cleanly, with an instruction,
not a traceback.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from spendtracker.config import Config
from spendtracker.ingest import loader

from .conftest import write_csv

OPTIONAL_PACKAGES = ("flask", "dateutil", "anthropic", "PIL")

REPO_ROOT = Path(__file__).resolve().parent.parent

RUNNER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {root!r})

    BLOCKED = {blocked!r}

    class Blocker:
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BLOCKED:
                raise ImportError(name + " is not installed (simulated)")
            return None

    sys.meta_path.insert(0, Blocker())

    from spendtracker.cli import main
    sys.exit(main(sys.argv[1:]))
    """
)


@pytest.fixture
def bare_runner(tmp_path: Path) -> Path:
    script = tmp_path / "bare.py"
    script.write_text(
        RUNNER.format(root=str(REPO_ROOT), blocked=set(OPTIONAL_PACKAGES)),
        encoding="utf-8",
    )
    return script


@pytest.fixture
def populated(tmp_path: Path, cfg: Config) -> Path:
    """A data directory with one statement already imported."""
    from spendtracker import db as dbmod, taxonomy

    csv_path = write_csv(
        tmp_path / "jan.csv",
        ["Date", "Description", "Amount", "Balance"],
        [
            ["05/01/2026", "CARD PURCHASE CHECKERS", "-1200.00", "38800.00"],
            ["06/01/2026", "SALARY ACB CREDIT", "40000.00", "78800.00"],
            ["20/01/2026", "DEBIT ORDER DISCOVERY", "-2450.00", "76350.00"],
        ],
    )
    conn = dbmod.connect(cfg.db_path)
    dbmod.init_db(conn)
    taxonomy.seed(conn)
    loader.import_statement(conn, csv_path, cfg=cfg)
    conn.close()
    return cfg.data_dir


def run_bare(runner: Path, data_dir: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(runner), "--data-dir", str(data_dir), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize(
    "command",
    [
        ("status",),
        ("statements",),
        ("report", "--period", "2026-01"),
        ("advice", "--period", "2026-01"),
        ("merchants",),
        ("recurring",),
        ("review",),
    ],
)
def test_core_commands_need_no_third_party_packages(
    bare_runner: Path, populated: Path, command: tuple[str, ...]
) -> None:
    result = run_bare(bare_runner, populated, *command)
    assert result.returncode == 0, (
        f"`{' '.join(command)}` failed without optional packages:\n{result.stderr}"
    )
    assert "Traceback" not in result.stderr


def test_inspect_needs_no_third_party_packages(
    bare_runner: Path, populated: Path, tmp_path: Path
) -> None:
    csv_path = write_csv(
        tmp_path / "feb.csv",
        ["Date", "Description", "Amount", "Balance"],
        [["03/02/2026", "CARD PURCHASE ENGEN", "-810.00", "75540.00"]],
    )
    result = run_bare(bare_runner, populated, "inspect", str(csv_path))
    assert result.returncode == 0, result.stderr
    assert "Nothing was written" in result.stdout


def test_import_needs_no_third_party_packages(
    bare_runner: Path, populated: Path, tmp_path: Path
) -> None:
    csv_path = write_csv(
        tmp_path / "feb.csv",
        ["Date", "Description", "Amount", "Balance"],
        [["03/02/2026", "CARD PURCHASE ENGEN", "-810.00", "75540.00"]],
    )
    result = run_bare(bare_runner, populated, "import-statement", str(csv_path))
    assert result.returncode == 0, result.stderr
    assert "1 row(s) imported" in result.stdout


def test_json_report_needs_no_third_party_packages(
    bare_runner: Path, populated: Path
) -> None:
    import json

    result = run_bare(bare_runner, populated, "report", "--period", "2026-01", "--json", "-")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["totals"]["total_outflow_cents"] == 365_000
    assert payload["reconciliation"]["reconciles"] is True


def test_serve_without_flask_explains_itself(
    bare_runner: Path, populated: Path
) -> None:
    """The one command that genuinely needs a package must say so, not crash."""
    result = run_bare(bare_runner, populated, "serve")

    assert result.returncode == 3
    assert "Traceback" not in result.stderr
    assert "Flask" in result.stderr
    assert "spendtracker[web]" in result.stderr
