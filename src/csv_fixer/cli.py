"""CLI entrypoint for local offline demos."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from csv_fixer.agent import CsvFixAgent
from csv_fixer.audit import AuditLog
from csv_fixer.config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sandboxed CSV fixer (local)")
    parser.add_argument("csv", type=Path, help="Input CSV path")
    parser.add_argument("contract", type=Path, help="Contract YAML path")
    parser.add_argument("--work", type=Path, default=None, help="Work directory")
    parser.add_argument("--json", action="store_true", help="Print full JSON result")
    args = parser.parse_args(argv)

    settings = load_settings()
    audit = AuditLog()
    agent = CsvFixAgent(settings=settings, audit=audit)
    result = agent.run(csv_path=args.csv, contract_path=args.contract, work_root=args.work)

    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2))
    else:
        print(f"job_id={result.job_id} success={result.success}")
        print(result.message)
        if result.fixed_csv_path:
            print(f"fixed={result.fixed_csv_path}")
        blocked = [e for e in result.audit if e.blocked]
        if blocked:
            print("containment events:")
            for e in blocked:
                print(f"  - {e.action}: {e.detail} ({e.block_reason})")
        for a in result.attempts:
            print(
                f"  attempt {a.attempt}: sandbox_ok={a.sandbox.ok} "
                f"exit={a.sandbox.exit_code} "
                f"valid={None if a.validation is None else a.validation.passed}"
            )
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
