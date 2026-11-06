# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Check the lifecycle logs and the canonical definition digests.

A lifecycle log is an append-only list of events over published case
definitions. Replaying it against the current definitions yields the
lifecycle projection: the status, digest and replacement of every case and
the definitions whose recorded results are tainted. This module replays and
checks logs, checks a log change against the previous revision, and checks
the digest vectors.

Run it to check the committed logs, examples and vectors, or to print a
projection.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import canonical, check_contract  # noqa: E402

ROOT = check_contract.ROOT
EXAMPLES = check_contract.EXAMPLES

# Each log covers one set of definitions; examples never mix with releasable cases.
SETS = {
    "examples": {"log": "examples/lifecycle.json", "cases": "examples/valid"},
    "conformance": {"log": "registry/lifecycle.json", "cases": "cases"},
}
PROJECTION = EXAMPLES / "lifecycle-projection.json"
INVALID_LOGS = EXAMPLES / "lifecycle-invalid.json"
DIGEST_VECTORS = EXAMPLES / "digests.json"

ACTIVE, DEPRECATED, INVALID = "active", "deprecated", "invalid"
TRANSITIONS = {
    "deprecate": ({ACTIVE}, DEPRECATED),
    "invalidate": ({ACTIVE, DEPRECATED}, INVALID),
    "reinstate": ({DEPRECATED}, ACTIVE),
    "revise": ({ACTIVE, DEPRECATED}, None),
}
UNTAINTED_REVISION = "representation"


def load_cases(directory):
    """Return the case documents under a directory, ordered by file path."""
    return [canonical.load_json(path) for path in sorted(Path(directory).rglob("*.json"))]


def load_set(name, root=ROOT):
    """Return the lifecycle log and the cases of one set in the working tree."""
    paths = SETS[name]
    log_path = Path(root) / paths["log"]
    log = canonical.load_json(log_path) if log_path.exists() else None
    return log, load_cases(Path(root) / paths["cases"])


def _chain(state, start):
    """Yield the replacement chain from a case, stopping at a repeat."""
    seen = set()
    while start is not None and start not in seen:
        seen.add(start)
        yield start
        start = state.get(start, {}).get("replacement")


def _expected_digests(events, digests):
    """Return, per event index, the digest the event must carry, and problems.

    Walking backwards from the current definitions gives each event the digest
    of the definition it applies to: a revise moves that digest back to the
    definition it revised.
    """
    tracked, expected, problems = dict(digests), {}, []
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        case_id = event["case"]
        if case_id not in tracked:
            continue
        if event["event"] == "revise":
            if event["revised_digest"] != tracked[case_id]:
                problems.append(("event-digest-mismatch", f"event {event['seq']}: {case_id} revised_digest"))
            tracked[case_id] = event["digest"]
        expected[index] = tracked[case_id]
    return expected, problems


def replay(log, cases):
    """Replay a schema-valid log over the current cases.

    Return the projection and the (code, detail) problems. The projection is a
    pure function of the definitions and the events, so anyone holding both
    reproduces it exactly.
    """
    digests = {case["id"]: canonical.digest(case) for case in cases}
    events = log["events"]
    expected, problems = _expected_digests(events, digests)
    state = {case_id: {"status": ACTIVE, "events": []} for case_id in digests}
    tainted = []
    for index, event in enumerate(events):
        seq, case_id, kind = event["seq"], event["case"], event["event"]
        label = f"event {seq}: {case_id}"
        if seq != index + 1:
            problems.append(("event-sequence", f"event at position {index + 1} has seq {seq}"))
        if case_id not in digests:
            problems.append(("event-case-unknown", label))
            continue
        if event["digest"] != expected[index]:
            problems.append(("event-digest-mismatch", label))
        review, actor = event["review"], event["actor"]
        reviewers = set(review["reviewers"])
        if review["status"] == "self-reviewed" and not reviewers <= {actor}:
            problems.append(("review-status-mismatch", f"{label}: self-reviewed by someone other than the actor"))
        if review["status"] == "peer-reviewed" and reviewers <= {actor}:
            problems.append(("review-not-independent", f"{label}: peer review needs a reviewer other than the actor"))
        entry = state[case_id]
        sources, target = TRANSITIONS[kind]
        if entry["status"] not in sources:
            problems.append(("transition-forbidden", f"{label}: {kind} of a {entry['status']} case"))
            continue
        entry["events"].append(seq)
        code = event["reason"]["code"]
        if kind == "revise":
            if log["phase"] == "stable" and code != UNTAINTED_REVISION:
                problems.append(("revision-after-stable", f"{label}: {code}"))
            if event["revised_digest"] == event["digest"]:
                problems.append(("revision-unchanged", label))
            if code != UNTAINTED_REVISION:
                tainted.append({"case": case_id, "digest": event["digest"], "event": seq})
            continue
        replacement = event.get("replacement")
        if replacement is not None:
            if replacement not in digests:
                problems.append(("replacement-unknown", f"{label}: {replacement}"))
            elif case_id in _chain(state, replacement):
                problems.append(("replacement-cycle", f"{label}: {replacement}"))
            elif state[replacement]["status"] != ACTIVE:
                problems.append(("replacement-inactive", f"{label}: {replacement} is {state[replacement]['status']}"))
        if kind == "invalidate":
            tainted.append({"case": case_id, "digest": event["digest"], "event": seq})
        entry["status"] = target
        if replacement is None:
            entry.pop("replacement", None)
        else:
            entry["replacement"] = replacement
    projected = []
    for case_id in sorted(digests):
        entry = state[case_id]
        item = {"id": case_id, "status": entry["status"], "digest": digests[case_id]}
        if "replacement" in entry:
            item["replacement"] = entry["replacement"]
        if entry["events"]:
            item["events"] = entry["events"]
        projected.append(item)
    for case_id in _duplicates(case["id"] for case in cases):
        problems.append(("duplicate-id", case_id))
    return {"phase": log["phase"], "cases": projected, "tainted": tainted}, problems


def _duplicates(values):
    seen, repeated = set(), []
    for value in values:
        if value in seen and value not in repeated:
            repeated.append(value)
        seen.add(value)
    return repeated


def check_log(log, cases):
    """Return the schema error paths, or the semantic codes, of a log over cases."""
    errors = check_contract.schema_errors("lifecycle", log)
    if errors:
        return "schema", sorted({"/" + "/".join(str(part) for part in error.absolute_path) for error in errors})
    return "semantic", sorted({code for code, _ in replay(log, cases)[1]})


def check_transition(old_log, new_log, old_cases, new_cases):
    """Return problems that make a change rewrite history.

    old_log and old_cases are the base revision or the last release. A log
    only grows, a definition is never deleted, and a definition that changes
    is revised by an event appended in the same change.
    """
    problems = []
    old_events = old_log["events"] if old_log else []
    if new_log is None:
        return [("log-removed", "")] if old_log else []
    if old_log and old_log["phase"] == "stable" and new_log["phase"] != "stable":
        problems.append(("phase-reverted", new_log["phase"]))
    new_events = new_log["events"]
    for index, event in enumerate(old_events):
        if index >= len(new_events):
            problems.append(("event-removed", f"event {event['seq']}"))
        elif new_events[index] != event:
            problems.append(("event-rewritten", f"event {event['seq']}"))
    appended = new_events[len(old_events):]
    old_digests = {case["id"]: canonical.digest(case) for case in old_cases}
    new_digests = {case["id"]: canonical.digest(case) for case in new_cases}
    for case_id, digest in sorted(old_digests.items()):
        if case_id not in new_digests:
            problems.append(("case-removed", case_id))
            continue
        own = [event for event in appended if event["case"] == case_id]
        if own and own[0]["digest"] != digest:
            problems.append(("event-digest-mismatch", f"event {own[0]['seq']}: {case_id} at the base revision"))
        if new_digests[case_id] != digest and not any(event["event"] == "revise" for event in own):
            problems.append(("definition-rewritten", case_id))
    return problems


def check_digest_vectors(vectors, valid):
    """Return failures of the canonicalization and digest vectors."""
    failures = []
    for vector in vectors["canonicalization"]:
        name = vector["name"]
        try:
            text = canonical.canonicalize(vector["input"])
        except canonical.CanonicalError as error:
            if "canonical" in vector or not vector.get("rejected"):
                failures.append(f"digest vector {name}: rejected: {error}")
            continue
        if vector.get("rejected"):
            failures.append(f"digest vector {name}: accepted input that must be rejected")
            continue
        sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if (text, sha256) != (vector["canonical"], vector["sha256"]):
            failures.append(f"digest vector {name}: canonical form or SHA-256 differs")
    for name, expected in vectors["cases"].items():
        if canonical.digest(valid[name]) != expected:
            failures.append(f"digest vector {name}: digest differs")
    for edit in vectors["edits"]:
        case = valid[edit["base"]]
        for mutation in edit["mutations"]:
            case = check_contract.apply_mutation(case, mutation)
        found = canonical.digest(case)
        base = vectors["cases"][edit["base"]]
        if found != edit["digest"] or (found == base) != (edit["effect"] == "unchanged"):
            failures.append(f"digest vector {edit['name']}: expected {edit['effect']} digest {edit['digest']}")
    return failures


def load_invalid_logs(log):
    """Return the invalid log mutations with their documents."""
    invalid = []
    for entry in canonical.load_json(INVALID_LOGS)["mutations"]:
        document = log
        for mutation in entry["mutations"]:
            document = check_contract.apply_mutation(document, mutation)
        invalid.append((entry, document))
    return invalid


def run():
    failures = []
    logs = {}
    for name in SETS:
        log, cases = load_set(name)
        if log is None:
            failures.append(f"{name}: lifecycle log {SETS[name]['log']} is missing")
            continue
        stage, found = check_log(log, cases)
        failures += [f"{name} lifecycle: {stage}: {problem}" for problem in found]
        logs[name] = (log, cases)
    if "examples" in logs and not failures:
        log, cases = logs["examples"]
        if replay(log, cases)[0] != canonical.load_json(PROJECTION):
            failures.append(f"examples: projection differs from {PROJECTION.relative_to(ROOT).as_posix()}")
        for entry, document in load_invalid_logs(log):
            stage, found = check_log(document, cases)
            expected = entry["violation"]
            if {stage: found} != {key: sorted(value) for key, value in expected.items()}:
                failures.append(f"invalid log {entry['name']}: expected {expected}, found {stage} {found}")
    valid, _ = check_contract.load_examples()
    failures += check_digest_vectors(canonical.load_json(DIGEST_VECTORS), valid)
    return failures


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--projection", choices=sorted(SETS), help="print the lifecycle projection of a set")
    args = parser.parse_args(argv)
    if args.projection:
        log, cases = load_set(args.projection)
        if log is None:
            print(f"lifecycle log {SETS[args.projection]['log']} is missing", file=sys.stderr)
            return 1
        projection, problems = replay(log, cases)
        for code, detail in problems:
            print(f"{code}: {detail}", file=sys.stderr)
        print(json.dumps(projection, indent=2, ensure_ascii=False))
        return 1 if problems else 0
    failures = run()
    for failure in failures:
        print(failure, file=sys.stderr)
    if not failures:
        counts = {name: len(load_set(name)[0]["events"]) for name in SETS}
        print("lifecycle ok: " + ", ".join(f"{name} log with {count} events" for name, count in counts.items()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
