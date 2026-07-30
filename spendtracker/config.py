"""Runtime configuration.

Values come from (highest priority first):
  1. environment variables
  2. config.local.json in the project root (gitignored, for your own settings)
  3. the defaults below
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


@dataclass
class Config:
    # Where the SQLite database and uploaded files live.
    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path | None = None
    uploads_dir: Path | None = None

    currency_symbol: str = "R"
    currency_code: str = "ZAR"

    # Till-slip reading: "claude" (AI vision), "tesseract" (local OCR),
    # or "manual" (no extraction; you type the details in).
    ocr_provider: str = "claude"
    anthropic_api_key: str | None = None
    ocr_model: str = "claude-opus-5"

    # Receipt <-> bank transaction matching tolerances.
    match_days_window: int = 4
    match_amount_tolerance_cents: int = 100

    # A transaction above this amount is never auto-flagged as frivolous
    # without a category reason; it gets reviewed instead.
    large_txn_cents: int = 200_000

    # Set true once you also import your credit card statements. Until then a
    # credit card repayment is the only visible trace of that card's spending,
    # so it is counted as spend; flipping this makes it a transfer instead so
    # the same money is not counted twice.
    credit_card_statements_imported: bool = False

    # Categories treated as non-negotiable when generating advice.
    essential_categories: tuple[str, ...] = (
        "Housing",
        "Utilities",
        "Insurance",
        "Medical",
        "Debt Repayment",
        "Education",
        "Tax",
        "Transfers",
        "Savings & Investment",
    )

    web_host: str = "127.0.0.1"
    web_port: int = 5000
    secret_key: str = "dev-only-change-me"

    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.data_dir = Path(self.data_dir)
        if self.db_path is None:
            self.db_path = self.data_dir / "spending.db"
        if self.uploads_dir is None:
            self.uploads_dir = self.data_dir / "uploads"
        self.db_path = Path(self.db_path)
        self.uploads_dir = Path(self.uploads_dir)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        (self.uploads_dir / "receipts").mkdir(parents=True, exist_ok=True)
        (self.uploads_dir / "statements").mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("data_dir", "db_path", "uploads_dir"):
            d[key] = str(d[key])
        d.pop("anthropic_api_key", None)  # never serialise secrets
        return d


_ENV_MAP = {
    "SPENDTRACKER_DATA_DIR": ("data_dir", str),
    "SPENDTRACKER_DB": ("db_path", str),
    "SPENDTRACKER_CURRENCY_SYMBOL": ("currency_symbol", str),
    "SPENDTRACKER_CURRENCY_CODE": ("currency_code", str),
    "SPENDTRACKER_OCR_PROVIDER": ("ocr_provider", str),
    "SPENDTRACKER_OCR_MODEL": ("ocr_model", str),
    "ANTHROPIC_API_KEY": ("anthropic_api_key", str),
    "SPENDTRACKER_HOST": ("web_host", str),
    "SPENDTRACKER_PORT": ("web_port", int),
    "SPENDTRACKER_SECRET_KEY": ("secret_key", str),
    "SPENDTRACKER_MATCH_DAYS": ("match_days_window", int),
    "SPENDTRACKER_MATCH_TOLERANCE_CENTS": ("match_amount_tolerance_cents", int),
}

_cached: Config | None = None


def load_config(*, refresh: bool = False, overrides: dict | None = None) -> Config:
    """Build the effective configuration."""
    global _cached
    if _cached is not None and not refresh and not overrides:
        return _cached

    values: dict = {}

    local = PROJECT_ROOT / "config.local.json"
    if local.exists():
        try:
            values.update(json.loads(local.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"config.local.json is not valid JSON: {exc}") from exc

    for env_name, (attr, caster) in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw not in (None, ""):
            values[attr] = caster(raw)

    if overrides:
        values.update(overrides)

    known = {f for f in Config.__dataclass_fields__}
    extra = {k: v for k, v in values.items() if k not in known}
    clean = {k: v for k, v in values.items() if k in known}
    if isinstance(clean.get("essential_categories"), list):
        clean["essential_categories"] = tuple(clean["essential_categories"])
    cfg = Config(**clean)
    cfg.extra.update(extra)

    if not overrides:
        _cached = cfg
    return cfg
