"""Loading and hashing ``screen.toml``.

The config hash is what makes a historical screen run interpretable. It is
computed over the *resolved values* rather than the raw file bytes, so
reformatting or editing a comment does not invalidate a comparison, while any
change that could alter a verdict does.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths


class ConfigError(RuntimeError):
    pass


# Sub-tables that participate in gating. Changing anything else (sync cadence,
# unmeasurable list) does not invalidate a run comparison.
_GATING_SECTIONS = (
    "gate0",
    "gate1",
    "gate2",
    "gate3",
    "gate4",
    "gate5",
    "tiers",
    "valuation",
)


def _canonical(obj: Any) -> Any:
    """Recursively sort mappings so the JSON encoding is stable."""
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_canonical(v) for v in obj]
    return obj


@dataclass(frozen=True)
class Config:
    data: dict[str, Any]
    source: Path
    config_hash: str
    gating_hash: str
    warnings: tuple[str, ...] = field(default_factory=tuple)

    # `data` is a plain mutable dict while the dataclass is frozen, so mutating
    # it after construction would leave `config_hash` describing the old values.
    # Build instances through `load()`; tests that need a synthetic config must
    # compute the hashes after any mutation.

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, path: str, default: Any = None) -> Any:
        """Fetch a dotted path, e.g. ``get('gate1.earnings_quality.min_roe')``."""
        node: Any = self.data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def is_financial_overrides(self) -> dict[str, Any]:
        return self.get("gate2.financial", {}) or {}

    def resolved(self) -> dict[str, Any]:
        """The gating subset, snapshotted into ``screen_run.config_json``."""
        return {k: self.data[k] for k in _GATING_SECTIONS if k in self.data}


def _hash(obj: Any) -> str:
    blob = json.dumps(_canonical(obj), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _validate(data: dict[str, Any]) -> tuple[str, ...]:
    """Catch mistakes that would silently produce a meaningless screen.

    A missing threshold is not a crash: the gate treats it as unavailable. But
    it must be loud, or an absent ROE floor quietly reads as "ROE fine".
    """
    warnings: list[str] = []

    def get(path: str) -> Any:
        node: Any = data
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    if get("gate0.data_sufficiency.min_annual_periods") is None:
        warnings.append("gate0.min_annual_periods is unset; no history floor enforced")

    for path in ("gate1.earnings_quality.min_ocf_to_ni",
                 "gate2.balance_sheet.max_net_debt_to_equity",
                 "gate3.profitability.min_roe",
                 "gate3.profitability.min_ebitda_margin",
                 "gate3.profitability.min_roic"):
        if get(path) is None:
            warnings.append(f"{path} is unset")

    # Gate 5 must block. An empty required_checks list would let a buy through
    # with nothing verified, which is the exact failure the gate exists to stop.
    if not get("gate5.integrity.required_checks"):
        warnings.append("gate5.integrity.required_checks is empty; integrity would not block")

    risk_free = get("valuation.inputs.risk_free_rate")
    if risk_free is None:
        warnings.append("valuation.inputs.risk_free_rate is unset; ROIC>WACC cannot be evaluated")
    elif not (0.0 < float(risk_free) < 0.25):
        warnings.append(f"risk_free_rate={risk_free} looks implausible for India")

    return tuple(warnings)


def load(path: str | Path | None = None) -> Config:
    p = Path(path) if path else paths.config_path()
    if not p.exists():
        raise ConfigError(f"missing config: {p}")
    data = tomllib.loads(p.read_text())
    return Config(
        data=data,
        source=p,
        config_hash=_hash(data),
        gating_hash=_hash({k: data[k] for k in _GATING_SECTIONS if k in data}),
        warnings=_validate(data),
    )