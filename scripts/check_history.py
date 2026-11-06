# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Check the working tree against a base revision for rewritten history.

The rules that depend on history cannot be checked from one revision: the
registry must not lower an allocation or repurpose an identity, a lifecycle
log only grows, and a case definition is neither deleted nor changed without
a revise event. CI runs this against the base of a pull request or the
previous head of a pushed branch.
"""

import argparse
from pathlib import Path
import subprocess
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import canonical, check_contract, check_lifecycle  # noqa: E402

ROOT = check_contract.ROOT
REGISTRY = "registry/registry.json"


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=False)


class Revision:
    """Reads corpus documents as they were at one Git revision."""

    def __init__(self, revision):
        result = _git("rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
        if result.returncode != 0:
            raise ValueError(f"unknown revision {revision}")
        self.commit = result.stdout.decode("ascii").strip()

    def load(self, path):
        """Return the document at a path, or None if it did not exist."""
        listed = _git("ls-tree", "--name-only", self.commit, "--", path)
        if not listed.stdout.strip():
            return None
        shown = _git("show", f"{self.commit}:{path}")
        if shown.returncode != 0:
            raise ValueError(shown.stderr.decode("utf-8", "replace").strip())
        return canonical.loads(shown.stdout.decode("utf-8"))

    def cases(self, directory):
        listed = _git("ls-tree", "-r", "--name-only", self.commit, "--", directory)
        paths = sorted(line for line in listed.stdout.decode("utf-8").splitlines() if line.endswith(".json"))
        return [self.load(path) for path in paths]


def check_history(base):
    """Return failures of the working tree relative to a base revision."""
    failures = []
    old_registry = base.load(REGISTRY)
    if old_registry is not None:
        new_registry = canonical.load_json(ROOT / REGISTRY)
        failures += [f"registry: {code}: {detail}"
                     for code, detail in check_contract.check_registry_transition(old_registry, new_registry)]
    for name, paths in check_lifecycle.SETS.items():
        old_log, old_cases = base.load(paths["log"]), base.cases(paths["cases"])
        new_log, new_cases = check_lifecycle.load_set(name)
        problems = check_lifecycle.check_transition(old_log, new_log, old_cases, new_cases)
        failures += [f"{name}: {code}: {detail}" for code, detail in problems]
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True, help="base revision, such as origin/main or a commit")
    args = parser.parse_args(argv)
    try:
        base = Revision(args.base)
        failures = check_history(base)
    except ValueError as error:
        failures = [str(error)]
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        print(f"history ok against {args.base}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
