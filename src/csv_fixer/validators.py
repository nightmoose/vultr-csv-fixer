"""DataContractGate validation adapter.

Prefers the real ``contractgate`` Python SDK (local pure-Python validator).
If the package is missing, falls back to a minimal column/type stub so the
local prototype still runs offline. The stub is NOT a substitute for the
gateway; see BUILD_NOTES.md for the real package hook.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Optional

import yaml

from csv_fixer.models import ValidationReport, ViolationOut

_CONTRACTGATE_AVAILABLE = False
try:
    from contractgate import Contract  # type: ignore
    from contractgate.validator import validate as cg_validate  # type: ignore

    _CONTRACTGATE_AVAILABLE = True
except Exception:  # pragma: no cover - exercised when package absent
    Contract = None  # type: ignore
    cg_validate = None  # type: ignore


def contractgate_available() -> bool:
    return _CONTRACTGATE_AVAILABLE


def load_contract_yaml(path: Path | str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("contract YAML must be a mapping")
    return data


def _rows_from_csv(path: Path) -> list[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def _coerce_row(row: dict[str, str], field_types: dict[str, str]) -> dict[str, Any]:
    """Best-effort CSV string → typed values for ontology checks."""
    out: dict[str, Any] = {}
    for key, raw in row.items():
        if raw is None or raw == "":
            out[key] = None
            continue
        t = field_types.get(key, "string")
        try:
            if t == "integer":
                out[key] = int(float(raw)) if "." in str(raw) else int(raw)
            elif t in {"float", "number"}:
                out[key] = float(raw)
            elif t == "boolean":
                out[key] = str(raw).strip().lower() in {"1", "true", "yes"}
            else:
                out[key] = raw
        except (TypeError, ValueError):
            out[key] = raw
    return out


def _field_types_from_contract(contract: dict[str, Any]) -> dict[str, str]:
    entities = (
        contract.get("ontology", {}).get("entities")
        or contract.get("entities")
        or []
    )
    return {e["name"]: str(e.get("type", "string")).lower() for e in entities if "name" in e}


def _stub_validate(contract: dict[str, Any], rows: list[dict[str, Any]]) -> ValidationReport:
    """Minimal offline validator when contractgate is not installed."""
    entities = (
        contract.get("ontology", {}).get("entities")
        or contract.get("entities")
        or []
    )
    violations: list[ViolationOut] = []
    for idx, row in enumerate(rows):
        for ent in entities:
            name = ent["name"]
            required = bool(ent.get("required", False))
            ftype = str(ent.get("type", "string")).lower()
            enum = ent.get("enum")
            val = row.get(name)
            if required and (val is None or val == ""):
                violations.append(
                    ViolationOut(
                        field=name,
                        message=f"row {idx}: required field '{name}' is missing",
                        kind="missing_required_field",
                    )
                )
                continue
            if val is None or val == "":
                continue
            if ftype == "integer" and not isinstance(val, int):
                violations.append(
                    ViolationOut(
                        field=name,
                        message=f"row {idx}: expected integer, got {type(val).__name__}",
                        kind="type_mismatch",
                    )
                )
            elif ftype in {"float", "number"} and not isinstance(val, (int, float)):
                violations.append(
                    ViolationOut(
                        field=name,
                        message=f"row {idx}: expected float, got {type(val).__name__}",
                        kind="type_mismatch",
                    )
                )
            if enum is not None and val not in enum:
                violations.append(
                    ViolationOut(
                        field=name,
                        message=f"row {idx}: value {val!r} not in enum {enum}",
                        kind="enum_violation",
                    )
                )
            if "min" in ent and isinstance(val, (int, float)) and val < ent["min"]:
                violations.append(
                    ViolationOut(
                        field=name,
                        message=f"row {idx}: value {val} below min {ent['min']}",
                        kind="range_violation",
                    )
                )
            if "max" in ent and isinstance(val, (int, float)) and val > ent["max"]:
                violations.append(
                    ViolationOut(
                        field=name,
                        message=f"row {idx}: value {val} above max {ent['max']}",
                        kind="range_violation",
                    )
                )
    return ValidationReport(
        passed=not violations,
        violations=violations,
        rows_checked=len(rows),
        backend="stub",
    )


def validate_csv_against_contract(
    csv_path: Path | str,
    contract_path: Path | str,
    *,
    max_rows: Optional[int] = None,
) -> ValidationReport:
    """Validate each CSV row as an event against the contract ontology."""
    csv_path = Path(csv_path)
    contract = load_contract_yaml(contract_path)
    field_types = _field_types_from_contract(contract)
    rows_raw = _rows_from_csv(csv_path)
    if max_rows is not None:
        rows_raw = rows_raw[:max_rows]
    rows = [_coerce_row(r, field_types) for r in rows_raw]

    if not _CONTRACTGATE_AVAILABLE:
        return _stub_validate(contract, rows)

    compiled = Contract.from_yaml(Path(contract_path).read_text(encoding="utf-8")).compile()
    violations: list[ViolationOut] = []
    for idx, row in enumerate(rows):
        # Drop None values so missing required fields surface correctly.
        event = {k: v for k, v in row.items() if v is not None}
        result = cg_validate(compiled, event)
        if not result.passed:
            for v in result.violations:
                violations.append(
                    ViolationOut(
                        field=v.field,
                        message=f"row {idx}: {v.message}",
                        kind=v.kind.value if hasattr(v.kind, "value") else str(v.kind),
                    )
                )
    return ValidationReport(
        passed=not violations,
        violations=violations,
        rows_checked=len(rows),
        backend="contractgate",
    )
