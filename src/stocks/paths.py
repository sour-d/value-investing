"""Filesystem locations.

Everything is resolved relative to the repository root so the tool behaves the
same regardless of the working directory it is invoked from.
"""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Repository root.

    Honours ``STOCKS_ROOT`` so tests can run against a temporary tree.
    """
    env = os.environ.get("STOCKS_ROOT")
    if env:
        return Path(env).resolve()
    # src/stocks/paths.py -> src/stocks -> src -> <root>
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    return repo_root() / "data"


def db_path() -> Path:
    return data_dir() / "stocks.db"


def raw_dir() -> Path:
    return data_dir() / "raw"


def config_path() -> Path:
    return repo_root() / "screen.toml"


def journal_dir() -> Path:
    return repo_root() / "journal"


def thesis_dir() -> Path:
    return repo_root() / "thesis"


def ensure_dirs() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    raw_dir().mkdir(parents=True, exist_ok=True)
    journal_dir().mkdir(parents=True, exist_ok=True)