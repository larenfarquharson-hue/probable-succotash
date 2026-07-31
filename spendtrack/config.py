"""Configuration: where data lives and how money is displayed."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

ENV_HOME = "SPENDTRACK_HOME"
DEFAULT_HOME = Path.home() / ".spendtrack"


def home_dir() -> Path:
    """Directory holding the database, rules overrides and slip drop-box."""
    raw = os.environ.get(ENV_HOME)
    return Path(raw).expanduser() if raw else DEFAULT_HOME


def db_path() -> Path:
    return home_dir() / "spendtrack.db"


def rules_path() -> Path:
    """User-editable category rules. Merged over the built-in defaults."""
    return home_dir() / "rules.json"


def slips_dir() -> Path:
    """Drop-box for slip images and slip JSON files."""
    return home_dir() / "slips"


@dataclass
class Settings:
    currency: str = "R"
    # Monthly take-home pay. Optional; used to express spend as a share of
    # income. None means those insights are skipped rather than guessed at.
    monthly_income: float | None = None
    # Spend below this rand value is treated as "small" for the
    # death-by-a-thousand-cuts insight.
    small_txn_threshold: float = 150.0
    # Days either side of a slip date when looking for its statement line.
    slip_match_window_days: int = 4
    # Rand tolerance when matching a slip total to a statement amount.
    slip_match_amount_tolerance: float = 0.05
    ignored_categories: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        path = home_dir() / "settings.json"
        if not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> Path:
        path = home_dir() / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return path


def ensure_dirs() -> None:
    home_dir().mkdir(parents=True, exist_ok=True)
    slips_dir().mkdir(parents=True, exist_ok=True)


def money(amount: float, currency: str = "R") -> str:
    """Format a rand value with thousands separators, negatives in brackets."""
    sign = "-" if amount < 0 else ""
    return f"{sign}{currency}{abs(amount):,.2f}"
