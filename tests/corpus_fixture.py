# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""A golden corpus tree for validator and release tests.

The repository has no conformance cases yet, so the fixture moves the example
cases into the conformance namespaces asn1.der and asn1.ber, with their
lifecycle log, and copies the committed schemas, registry and license. It is
derived from committed data, so it tracks every change to the examples.
"""

import copy
import json
from pathlib import Path
import shutil

from scripts import canonical, check_contract, check_lifecycle

ROOT = check_contract.ROOT
NAMESPACES = {"example.der": "asn1.der", "example.ber": "asn1.ber"}
REVISION = "0123456789abcdef0123456789abcdef01234567"


def conformance_id(case_id):
    namespace, serial = case_id.rsplit(".", 1)
    return f"{NAMESPACES[namespace]}.{serial}"


def case_path(case_id):
    return f"cases/{case_id.rsplit('.', 1)[0]}/{case_id}.json"


def write_json(path, document, ensure_ascii=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((json.dumps(document, indent=2, ensure_ascii=ensure_ascii) + "\n").encode("utf-8"))


def read_json(path):
    return canonical.load_json(path)


def conformance_set():
    """Return the example cases and lifecycle log moved into conformance namespaces."""
    log, examples = check_lifecycle.load_set("examples")
    cases = []
    for example in examples:
        case = copy.deepcopy(example)
        case["id"] = conformance_id(case["id"])
        if "related" in case["informative"]:
            case["informative"]["related"] = [conformance_id(related) for related in case["informative"]["related"]]
        cases.append(case)
    digests = {case["id"]: canonical.digest(case) for case in cases}
    log = copy.deepcopy(log)
    for event in log["events"]:
        event["case"] = conformance_id(event["case"])
        event["digest"] = digests[event["case"]]
        if "replacement" in event:
            event["replacement"] = conformance_id(event["replacement"])
    return cases, log


def write_corpus(directory):
    """Write the fixture corpus into a new directory and return its path."""
    directory = Path(directory)
    shutil.copytree(ROOT / "schemas", directory / "schemas")
    shutil.copyfile(ROOT / "LICENSE", directory / "LICENSE")
    registry = read_json(ROOT / "registry" / "registry.json")
    cases, log = conformance_set()
    for namespace in registry["namespaces"]:
        serials = [check_contract.parse_id(case["id"])[1] for case in cases
                   if check_contract.parse_id(case["id"])[0] == namespace["id"]]
        if serials:
            namespace["allocated"] = max(serials)
    write_json(directory / "registry" / "registry.json", registry)
    write_json(directory / "registry" / "lifecycle.json", log)
    for case in cases:
        write_json(directory / case_path(case["id"]), case)
    return directory
