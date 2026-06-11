#!/usr/bin/env python3
"""Service-independence contract completeness check.

``.importlinter``'s ``service-independence`` contract is an ``independence``
contract over a hand-enumerated ``modules`` list. Hand-maintained lists rot: a
new service module is easy to add under ``py_modules/services/`` while forgetting
to enrol it, and a renamed or removed service leaves a stale entry behind. Either
way the flagship layer-boundary enforcement silently stops covering reality.

This check derives the expected service set from the filesystem and fails when
``.importlinter`` and the directory diverge — making the list self-healing.

Derivation rule (a "service" = one independence entry):

  * every ``py_modules/services/<name>.py`` except ``__init__.py`` -> ``services.<name>``
  * every ``py_modules/services/<dir>/`` package (has ``__init__.py``) except
    ``protocols/`` -> ``services.<dir>``

A sub-package (e.g. ``services.saves``, ``services.library``) counts as ONE entry,
not one-per-file: the sub-services inside a bounded context may import each other
concretely (the documented carve-out in CLAUDE.md); independence is enforced
between the top-level entries, so the contract lists the package, not its
internals. ``services.protocols`` is not a service — it is the Protocol namespace
services depend on — so it is excluded.

``EXEMPT`` holds services deliberately kept out of the contract. It is empty
today; an entry here is a conscious "this service is intentionally not subject to
the independence contract" decision — the alternative the failure message offers
to simply enrolling the service.

Exit 0 when the contract matches the filesystem, 1 (one line per discrepancy)
otherwise.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO_ROOT / "py_modules" / "services"
IMPORTLINTER_PATH = REPO_ROOT / ".importlinter"

CONTRACT_NAME = "service-independence"
CONTRACT_SECTION = f"importlinter:contract:{CONTRACT_NAME}"
CONTRACT_KEY = "modules"

# Sub-directories under services/ that are NOT services in their own right.
_NON_SERVICE_DIRS = {"protocols"}

# Services deliberately excluded from the independence contract. Empty today;
# add an entry only as a conscious decision (see the module docstring).
EXEMPT: set[str] = set()


def derive_services(services_dir: Path) -> set[str]:
    """Return the service module names the independence contract should enumerate."""
    services: set[str] = set()
    if not services_dir.is_dir():
        return services
    for entry in sorted(services_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".py" and entry.stem != "__init__":
            services.add(f"services.{entry.stem}")
        elif entry.is_dir() and entry.name not in _NON_SERVICE_DIRS and (entry / "__init__.py").is_file():
            services.add(f"services.{entry.name}")
    return services


def parse_contract_modules(importlinter_path: Path) -> set[str]:
    """Read the ``modules`` list of the service-independence contract from ``.importlinter``."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(importlinter_path, encoding="utf-8")
    raw = parser.get(CONTRACT_SECTION, CONTRACT_KEY, fallback="")
    return {token.strip() for token in raw.split() if token.strip()}


def find_discrepancies(on_disk: set[str], contract: set[str], exempt: set[str]) -> list[str]:
    """Compare the derived service set against the contract. One message per discrepancy."""
    expected = on_disk - exempt
    findings: list[str] = [
        f"{missing}: service module exists but is not enrolled in the [{CONTRACT_NAME}] "
        f"contract — add it to .importlinter, or add it to EXEMPT in this script if it is "
        f"intentionally outside the contract."
        for missing in sorted(expected - contract)
    ]
    findings.extend(
        f"{stale}: listed in the [{CONTRACT_NAME}] contract but no such service module exists "
        f"under py_modules/services/ — remove the stale entry (or fix a rename)."
        for stale in sorted(contract - on_disk)
    )
    findings.extend(
        f"{contradiction}: marked EXEMPT in this script but also listed in the contract — remove it from one."
        for contradiction in sorted(exempt & contract)
    )
    return findings


def main(argv: list[str]) -> int:
    if any(a in {"-h", "--help"} for a in argv):
        print(__doc__)
        return 0
    on_disk = derive_services(SERVICES_DIR)
    contract = parse_contract_modules(IMPORTLINTER_PATH)
    findings = find_discrepancies(on_disk, contract, EXEMPT)
    if findings:
        for line in findings:
            print(line)
        print()
        print(
            f"ERROR: the [{CONTRACT_NAME}] contract in .importlinter has drifted from "
            "py_modules/services/. Every service must be enrolled (or explicitly EXEMPT) so the "
            "independence enforcement keeps covering reality."
        )
        return 1
    print(f"OK: [{CONTRACT_NAME}] contract matches {SERVICES_DIR.relative_to(REPO_ROOT)} ({len(contract)} services).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
