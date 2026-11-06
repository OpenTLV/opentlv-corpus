# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Execution plans: select checks and resolve them against a release and an adapter.

An execution plan is a declarative JSON document that selects checks by
standard edition, rules, operation, strictness, view, tags, lifecycle status,
explicit IDs and a named suite of the registry. It never selects by results,
and it has no comparison mode: the corpus oracle is always the verdict
reference. Resolving a plan against one verified data release and one
adapter's capability descriptor gives the resolved execution plan: the exact
checks with their definition digests and applicability decisions, and the
identities of the plan, corpus, adapter and tooling and the effective limits.
It contains no time, so the same inputs always give the same document, and
a later release never changes it. Before running, the resolved plan is
checked against the release and the adapter actually given.

docs/execution-plan.md specifies plans and their resolution.
"""

import argparse
from dataclasses import dataclass, fields
import hashlib
import json
from pathlib import Path
import sys
from typing import Optional

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import canonical, check_contract, protocol, release  # noqa: E402

PLAN = "urn:opentlv:corpus:schema:plan:1"
RESOLUTION = "urn:opentlv:corpus:schema:resolution:1"
PROTOCOLS = (protocol.VERSION,)
APPLICABLE, UNSUPPORTED = "applicable", "unsupported"


class PlanError(Exception):
    """A document is not a plan or resolved plan this tooling reads; problems are (code, detail) pairs."""

    def __init__(self, problems):
        super().__init__("; ".join(f"{code}: {detail}" for code, detail in problems))
        self.problems = problems


def json_digest(document):
    """Return sha256:<hex> of the RFC 8785 canonical JSON of a document."""
    return canonical.DIGEST_PREFIX + hashlib.sha256(canonical.canonical_bytes(document)).hexdigest()


class _Members:
    """JSON conversion of a dataclass whose members are strings, integers or lists of strings.

    An absent member is None, so converting a document and back gives the
    same members, and the same canonical JSON.
    """

    @classmethod
    def from_json(cls, document):
        return cls(**{name: tuple(value) if isinstance(value, list) else value for name, value in document.items()})

    def to_json(self):
        members = ((field.name, getattr(self, field.name)) for field in fields(self))
        return {name: list(value) if isinstance(value, tuple) else value for name, value in members
                if value is not None}


@dataclass(frozen=True)
class Selection(_Members):
    """Conditions a check must meet; an absent condition holds for every check."""

    suite: Optional[str] = None
    cases: Optional[tuple] = None
    standards: Optional[tuple] = None
    rules: Optional[tuple] = None
    operations: Optional[tuple] = None
    strictness: Optional[tuple] = None
    views: Optional[tuple] = None
    tags: Optional[tuple] = None
    exclude_tags: Optional[tuple] = None
    lifecycle: Optional[tuple] = None

    def selects(self, case, key, status):
        """Whether the check <id>/<key> of a case with a lifecycle status meets every condition except the suite."""
        return check_contract.selects(self.to_json(), case, key, status)


@dataclass(frozen=True)
class Limits(_Members):
    """Harness limits in integer milliseconds and octets; None takes the protocol default."""

    hello_timeout_ms: Optional[int] = None
    execute_timeout_ms: Optional[int] = None
    exit_timeout_ms: Optional[int] = None
    max_message_octets: Optional[int] = None
    stderr_octets: Optional[int] = None

    def effective(self):
        """Return the limits with every default filled in."""
        default = protocol.Limits()
        values = {"hello_timeout_ms": round(default.hello_timeout * 1000),
                  "execute_timeout_ms": round(default.execute_timeout * 1000),
                  "exit_timeout_ms": round(default.exit_timeout * 1000),
                  "max_message_octets": default.max_message_octets, "stderr_octets": default.stderr_octets}
        return Limits(**{name: value if getattr(self, name) is None else getattr(self, name)
                         for name, value in values.items()})

    def harness(self):
        """Return the protocol.Limits a session enforces."""
        limits = self.effective()
        return protocol.Limits(hello_timeout=limits.hello_timeout_ms / 1000,
                               execute_timeout=limits.execute_timeout_ms / 1000,
                               exit_timeout=limits.exit_timeout_ms / 1000,
                               max_message_octets=limits.max_message_octets, stderr_octets=limits.stderr_octets)


@dataclass(frozen=True)
class CorpusPin(_Members):
    """A data release a plan must be resolved against: its version and the SHA-256 of its archive."""

    release: str
    sha256: str


@dataclass(frozen=True)
class ExecutionPlan:
    """A declarative plan document."""

    name: str
    select: Selection
    description: Optional[str] = None
    corpus: Optional[CorpusPin] = None
    limits: Optional[Limits] = None

    @classmethod
    def from_json(cls, document):
        return cls(name=document["name"], select=Selection.from_json(document["select"]),
                   description=document.get("description"),
                   corpus=CorpusPin.from_json(document["corpus"]) if "corpus" in document else None,
                   limits=Limits.from_json(document["limits"]) if "limits" in document else None)

    def to_json(self):
        document = {"schema": PLAN, "name": self.name}
        if self.description is not None:
            document["description"] = self.description
        if self.corpus is not None:
            document["corpus"] = self.corpus.to_json()
        document["select"] = self.select.to_json()
        if self.limits is not None:
            document["limits"] = self.limits.to_json()
        return document

    @property
    def digest(self):
        """The digest of the plan document's canonical JSON, whatever its formatting."""
        return json_digest(self.to_json())


@dataclass(frozen=True)
class PlanRef(_Members):
    name: str
    digest: str


@dataclass(frozen=True)
class CorpusRef(_Members):
    release: str
    sha256: str
    revision: str


@dataclass(frozen=True)
class Component(_Members):
    name: str
    version: str


@dataclass(frozen=True)
class AdapterRef:
    """The identity of an adapter and the digest of its complete capability descriptor."""

    protocol: int
    adapter: Component
    implementation: Component
    descriptor: str

    @classmethod
    def from_descriptor(cls, descriptor):
        return cls(descriptor["protocol"], Component.from_json(descriptor["adapter"]),
                   Component.from_json(descriptor["implementation"]), json_digest(descriptor))

    @classmethod
    def from_json(cls, document):
        return cls(document["protocol"], Component.from_json(document["adapter"]),
                   Component.from_json(document["implementation"]), document["descriptor"])

    def to_json(self):
        return {"protocol": self.protocol, "adapter": self.adapter.to_json(),
                "implementation": self.implementation.to_json(), "descriptor": self.descriptor}

    def __str__(self):
        return (f"{self.adapter.name} {self.adapter.version} for {self.implementation.name} "
                f"{self.implementation.version}")


@dataclass(frozen=True)
class Check(_Members):
    """One selected check, with the definition it was resolved against and its applicability decision."""

    case: str
    assertion: str
    digest: str
    status: str
    applicability: str
    problem: Optional[str] = None
    detail: Optional[str] = None

    @property
    def id(self):
        return f"{self.case}/{self.assertion}"


@dataclass(frozen=True)
class ResolvedExecutionPlan:
    """An execution plan resolved against one data release and one adapter."""

    plan: PlanRef
    corpus: CorpusRef
    tools: Component
    adapter: AdapterRef
    limits: Limits
    checks: tuple

    @classmethod
    def from_json(cls, document):
        return cls(PlanRef.from_json(document["plan"]), CorpusRef.from_json(document["corpus"]),
                   Component.from_json(document["tools"]), AdapterRef.from_json(document["adapter"]),
                   Limits.from_json(document["limits"]), tuple(Check.from_json(item) for item in document["checks"]))

    def to_json(self):
        return {"schema": RESOLUTION, "plan": self.plan.to_json(), "corpus": self.corpus.to_json(),
                "tools": self.tools.to_json(), "adapter": self.adapter.to_json(), "limits": self.limits.to_json(),
                "checks": [check.to_json() for check in self.checks]}

    def dumps(self):
        """Return the document as indented UTF-8 JSON with a final line feed."""
        return release.json_bytes(self.to_json())

    @property
    def identity(self):
        """The digest of the resolved plan's canonical JSON; serializing and loading it again keeps it."""
        return json_digest(self.to_json())


def _check_document(kind, urn, document):
    declared = document.get("schema") if isinstance(document, dict) else None
    if declared != urn:
        raise PlanError([("schema-unsupported", f"{declared!r} is not {urn}")])
    errors = check_contract.schema_errors(kind, document)
    if errors:
        raise PlanError(sorted({("schema", f"/{'/'.join(str(part) for part in error.absolute_path)} "
                                           f"({error.validator})") for error in errors}))
    try:
        canonical.canonicalize(document)
    except canonical.CanonicalError as error:
        raise PlanError([("not-canonicalizable", str(error))]) from error


def load_plan(document):
    """Return the ExecutionPlan of a parsed plan document, or raise PlanError."""
    _check_document("plan", PLAN, document)
    return ExecutionPlan.from_json(document)


def load_resolved(document):
    """Return the ResolvedExecutionPlan of a parsed document, or raise PlanError."""
    _check_document("resolution", RESOLUTION, document)
    return ResolvedExecutionPlan.from_json(document)


def read(path, loader):
    """Read a JSON file and load it with load_plan or load_resolved, or raise PlanError."""
    try:
        document = canonical.load_json(path)
    except (OSError, ValueError) as error:
        raise PlanError([("json-invalid", f"{path}: {error}")]) from error
    return loader(document)


def check_descriptor(descriptor):
    """Return the (code, detail) problems that keep a capability descriptor from deciding applicability."""
    version = descriptor.get("protocol") if isinstance(descriptor, dict) else None
    if version not in PROTOCOLS:
        return [("protocol-unsupported", f"protocol {version!r}; this tooling speaks {', '.join(map(str, PROTOCOLS))}")]
    errors = protocol.schema_errors(protocol.CAPABILITIES, descriptor)
    if errors:
        return [("descriptor-invalid", ", ".join(errors))]
    try:
        canonical.canonicalize(descriptor)
    except canonical.CanonicalError as error:
        return [("descriptor-invalid", str(error))]
    return []


def _corpus(release_):
    """Return the conformance cases by ID, their digests and their lifecycle statuses."""
    conformance = release_.corpus.sets["conformance"]
    cases = {case["id"]: case for case in conformance.cases.values()}
    digests = {conformance.cases[path]["id"]: digest for path, digest in conformance.digests.items()}
    statuses = {entry["id"]: entry["status"] for entry in conformance.projection["cases"]}
    return cases, digests, statuses


def _decide(case, key, digest, status, descriptor):
    assertion = next(item for item in case["normative"]["assertions"] if item["key"] == key)
    decision = protocol.applicability(descriptor, protocol.execute_params(case, key), assertion.get("requires", ()))
    if decision is None:
        return Check(case["id"], key, digest, status, APPLICABLE)
    return Check(case["id"], key, digest, status, UNSUPPORTED, *decision)


def _refs(selection, cases):
    problems = []
    for ref in selection.cases or ():
        case_id, _, key = ref.partition("/")
        if case_id not in cases:
            problems.append(("case-unknown", ref))
        elif key and key not in [assertion["key"] for assertion in cases[case_id]["normative"]["assertions"]]:
            problems.append(("assertion-unknown", ref))
    return problems


def resolve(plan, release_, descriptor):
    """Resolve a plan against a verified release.Release and an adapter's capability descriptor.

    Return the ResolvedExecutionPlan and no problems, or None and the
    (code, detail) problems that keep the plan from running. Every selected
    check is kept, including those the descriptor does not cover, which are
    recorded as unsupported with the protocol's problem code.
    """
    problems = []
    if plan.corpus is not None and (plan.corpus.release, plan.corpus.sha256) != (release_.release, release_.sha256):
        problems.append(("corpus-mismatch", f"the plan pins {plan.corpus.release} {plan.corpus.sha256}, "
                                            f"the release is {release_.release} {release_.sha256}"))
    problems += check_descriptor(descriptor)
    registry = release_.corpus.registry
    cases, digests, statuses = _corpus(release_)
    selections = [plan.select]
    if plan.select.suite is not None:
        suite = next((item for item in registry["suites"] if item["id"] == plan.select.suite), None)
        if suite is None:
            problems.append(("suite-unknown", plan.select.suite))
        else:
            selections.append(Selection.from_json(suite["select"]))
    problems += check_contract.check_selection(plan.select.to_json(), registry)
    problems += _refs(plan.select, cases)
    if problems:
        return None, problems

    checks = []
    for case_id in sorted(cases):
        for assertion in cases[case_id]["normative"]["assertions"]:
            key = assertion["key"]
            if all(selection.selects(cases[case_id], key, statuses[case_id]) for selection in selections):
                checks.append(_decide(cases[case_id], key, digests[case_id], statuses[case_id], descriptor))
    chosen = {check.id for check in checks} | {check.case for check in checks}
    for ref in plan.select.cases or ():
        if ref not in chosen:
            status = statuses[ref.partition("/")[0]]
            problems.append(("explicit-excluded", f"{ref} ({status}) is named but the other conditions exclude it"))
    if not checks and not problems:
        problems.append(("selection-empty", f"plan {plan.name} selects no check"))
    if problems:
        return None, problems
    resolved = ResolvedExecutionPlan(
        PlanRef(plan.name, plan.digest), CorpusRef(release_.release, release_.sha256, release_.revision),
        Component(release.TOOLS["name"], release.TOOLS["version"]), AdapterRef.from_descriptor(descriptor),
        (plan.limits or Limits()).effective(), tuple(checks))
    return resolved, []


def check_resolved(resolved, release_, descriptor):
    """Return the (code, detail) problems that keep a resolved plan from running against a release and an adapter.

    The release and the descriptor must be the ones the plan was resolved
    against, and every check must still be what resolution decided, so a
    resolved plan runs exactly as resolved or not at all.
    """
    problems = []
    pinned = resolved.corpus
    if (pinned.release, pinned.sha256, pinned.revision) != (release_.release, release_.sha256, release_.revision):
        problems.append(("corpus-mismatch", f"resolved against {pinned.release} {pinned.sha256}, "
                                            f"given {release_.release} {release_.sha256}"))
    if resolved.adapter.protocol not in PROTOCOLS:
        problems.append(("protocol-unsupported", f"protocol {resolved.adapter.protocol}"))
    descriptor_problems = check_descriptor(descriptor)
    problems += descriptor_problems
    if not descriptor_problems and json_digest(descriptor) != resolved.adapter.descriptor:
        problems.append(("adapter-mismatch", f"resolved for {resolved.adapter}, given "
                                             f"{AdapterRef.from_descriptor(descriptor)} or a changed descriptor"))
    if problems:
        return problems
    cases, digests, statuses = _corpus(release_)
    positions = {}
    for case_id, case in cases.items():
        for index, assertion in enumerate(case["normative"]["assertions"]):
            positions[f"{case_id}/{assertion['key']}"] = (case_id, index)
    previous = None
    for check in resolved.checks:
        if check.id not in positions:
            problems.append(("check-unknown", check.id))
            continue
        if previous is not None and positions[check.id] <= previous:
            problems.append(("check-order", f"{check.id} is repeated or out of order"))
        previous = positions[check.id]
        expected = _decide(cases[check.case], check.assertion, digests[check.case], statuses[check.case], descriptor)
        if check != expected:
            problems.append(("check-mismatch", f"{check.id}: resolution gives {json.dumps(expected.to_json())}"))
    return problems


def describe(resolved):
    """Return a one-line summary with the counts of applicable and unsupported checks."""
    counts = {state: sum(check.applicability == state for check in resolved.checks)
              for state in (APPLICABLE, UNSUPPORTED)}
    return (f"plan {resolved.plan.name}: {len(resolved.checks)} checks, {counts[APPLICABLE]} applicable, "
            f"{counts[UNSUPPORTED]} unsupported; corpus {resolved.corpus.release}; adapter {resolved.adapter}; "
            f"identity {resolved.identity}")


def _descriptor(args):
    """Return the descriptor from --descriptor or from the handshake of the adapter command, or raise PlanError."""
    if args.descriptor:
        try:
            return canonical.load_json(args.descriptor)
        except (OSError, ValueError) as error:
            raise PlanError([("json-invalid", f"{args.descriptor}: {error}")]) from error
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    session = protocol.Session(command)
    try:
        session.start()
        return session.descriptor
    except protocol.SessionError as error:
        raise PlanError([("adapter-" + error.code, error.detail)]) from error
    finally:
        session.close()


def _release(args):
    opened, problems = release.open_release(args.corpus, args.sums)
    if problems:
        raise PlanError([("release-invalid", str(problem)) for problem in problems])
    return opened


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command_name", required=True)
    usage = "%(prog)s --corpus ARCHIVE [--sums SHA256SUMS] {} (--descriptor FILE | -- COMMAND [ARGUMENT ...])"
    resolve_parser = commands.add_parser("resolve", help="resolve a plan against a release and an adapter",
                                         usage=usage.format("--plan PLAN [--output FILE]"))
    resolve_parser.add_argument("--plan", required=True, help="the plan document")
    resolve_parser.add_argument("--output", help="file to write the resolved plan to; standard output by default")
    check_parser = commands.add_parser("check", help="check a resolved plan before running it",
                                       usage=usage.format("--resolved FILE"))
    check_parser.add_argument("--resolved", required=True, help="the resolved plan document")
    for sub in (resolve_parser, check_parser):
        sub.add_argument("--corpus", required=True, help="the data release archive")
        sub.add_argument("--sums", help="the SHA256SUMS published with the archive")
        sub.add_argument("--descriptor", help="the adapter's capability descriptor, instead of starting the adapter")
        sub.add_argument("command", nargs=argparse.REMAINDER, help="the adapter command, after --")
    args = parser.parse_args(argv)
    if bool(args.descriptor) == bool([word for word in args.command if word != "--"]):
        parser.error("give either --descriptor or the adapter command after --")
    try:
        opened = _release(args)
        descriptor = _descriptor(args)
        if args.command_name == "resolve":
            resolved, problems = resolve(read(args.plan, load_plan), opened, descriptor)
        else:
            resolved = read(args.resolved, load_resolved)
            problems = check_resolved(resolved, opened, descriptor)
    except PlanError as error:
        problems = error.problems
    for code, detail in problems:
        print(f"{code}: {detail}", file=sys.stderr)
    if problems:
        print(f"plan not runnable: {len(problems)} problems", file=sys.stderr)
        return 1
    if args.command_name == "resolve":
        if args.output:
            Path(args.output).write_bytes(resolved.dumps())
        else:
            sys.stdout.buffer.write(resolved.dumps())
            sys.stdout.flush()
    print(describe(resolved), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
