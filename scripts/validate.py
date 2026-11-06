# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Validate a corpus tree: the source repository or an extracted data release.

The validator runs every check one revision can answer: encoding and JSON
syntax of every document, the schema files, the registry, each case set with
its IDs, paths, references, digests and licenses, the lifecycle logs and the
published release records. Each problem is one diagnostic,
"<path>: <code>: <detail>", and the diagnostics are sorted, so two runs over
the same files print the same bytes.

Run it with a corpus root, the repository by default.
"""

import argparse
import codecs
from collections import namedtuple
import hashlib
from pathlib import Path
import re
import sys

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import canonical, check_contract, check_lifecycle  # noqa: E402

ROOT = check_contract.ROOT

SCHEMA_URN = "urn:opentlv:corpus:schema:{kind}:{version}"
SCHEMA_FILE = re.compile(r"([a-z]+)-([1-9][0-9]*)\.schema\.json")
# Document kinds and the schema version of each that this tooling checks.
SUPPORTED = {"case": 1, "registry": 1, "lifecycle": 1, "manifest": 1}
DOCUMENT_DIRS = ("schemas", "registry", "cases", "examples", "releases")
REGISTRY = "registry/registry.json"
LICENSE = "LICENSE"
# The namespace purpose of each case set; only the conformance set is required.
PURPOSES = {"conformance": "conformance", "examples": "example"}
REQUIRED_SETS = ("conformance",)
CONFORMANCE_PATH = "cases/{namespace}/{id}.json"

RELEASES = "releases"
RELEASE_MANIFEST = "manifest.json"
RELEASE_SUMS = "SHA256SUMS"
ARCHIVE_PREFIX = "opentlv-corpus-data-"
# Semantic Versioning 2.0.0 without build metadata, so a version is a safe file name.
_IDENTIFIER = r"(0|[1-9][0-9]*|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)"
VERSION = re.compile(rf"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-{_IDENTIFIER}(\.{_IDENTIFIER})*)?")
SUMS_LINE = re.compile(r"([0-9a-f]{64})  ([^\n]+)")


class Diagnostic(namedtuple("Diagnostic", "path code detail")):
    """One problem: the file relative to the corpus root, a stable code and a detail."""

    __slots__ = ()

    def __str__(self):
        return f"{self.path}: {self.code}: {self.detail}" if self.detail else f"{self.path}: {self.code}"


CaseSet = namedtuple("CaseSet", "name cases digests log projection")
CaseSet.__doc__ = "Schema-valid cases and their definition digests by path, the lifecycle log and its projection."


class Corpus:
    """The result of validating a corpus tree."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = []
        self.documents = {}
        self.validators = {}
        self.registry = None
        self.sets = {}
        self._diagnostics = set()

    @property
    def diagnostics(self):
        return sorted(self._diagnostics)

    def report(self, path, code, detail=""):
        self._diagnostics.add(Diagnostic(path, code, detail))

    def read(self, path):
        """Return the parsed document at a path, or None after reporting why it cannot be read."""
        if path not in self.documents:
            if not (self.root / path).is_file():
                self.report(path, "file-missing")
                return None
            document, problems = read_document((self.root / path).read_bytes())
            self.documents[path] = document
            for code, detail in problems:
                self.report(path, code, detail)
        return self.documents[path]

    def document(self, path, kind):
        """Return a document valid against its schema version, or None after reporting why it is not."""
        document = self.read(path)
        if document is None:
            return None
        expected = SCHEMA_URN.format(kind=kind, version=SUPPORTED[kind])
        declared = document.get("schema") if isinstance(document, dict) else None
        if declared != expected:
            self.report(path, "schema-unsupported", f"{declared!r} is not {expected}")
            return None
        validator = self.validators.get(expected)
        if validator is None:
            return None
        errors = {(_pointer(error.absolute_path), error.validator) for error in validator.iter_errors(document)}
        for pointer, keyword in errors:
            self.report(path, "schema", f"{pointer} ({keyword})")
        return None if errors else document


def archive_base(version):
    """Return the file name stem of the release artifacts of a version."""
    return ARCHIVE_PREFIX + version


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def format_sums(digests):
    """Return SHA256SUMS text for {name: hex digest}, ordered by name, as sha256sum writes it."""
    return "".join(f"{digests[name]}  {name}\n" for name in sorted(digests)).encode("ascii")


def parse_sums(data):
    """Return {name: hex digest} of SHA256SUMS octets, or None if they are malformed."""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None
    digests = {}
    for line in text.splitlines(keepends=True):
        match = SUMS_LINE.fullmatch(line.rstrip("\n")) if line.endswith("\n") else None
        if match is None or match.group(2) in digests:
            return None
        digests[match.group(2)] = match.group(1)
    return digests


def read_text(data):
    """Return the text of octets and the (code, detail) encoding problems.

    Corpus files are UTF-8 without a byte order mark, end lines with LF and
    end with a line feed, so their bytes, and the release archive that holds
    them, do not depend on the platform that checked them out. The text is
    None when the octets are not UTF-8.
    """
    problems = []
    if data.startswith(codecs.BOM_UTF8):
        problems.append(("encoding-bom", "a byte order mark precedes the text"))
        data = data[len(codecs.BOM_UTF8):]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        return None, problems + [("encoding-invalid", f"not UTF-8 at octet {error.start}")]
    if "\r" in text:
        problems.append(("encoding-line-ending", "a carriage return; lines end with LF only"))
    if not text.endswith("\n"):
        problems.append(("encoding-final-newline", "the text does not end with a line feed"))
    return text, problems


def read_document(data):
    """Return the document parsed from JSON octets and the (code, detail) encoding problems.

    The document is None when it cannot be parsed, including repeated members
    and NaN or Infinity.
    """
    text, problems = read_text(data)
    if text is None:
        return None, problems
    try:
        return canonical.loads(text), problems
    except ValueError as error:
        return None, problems + [("json-invalid", str(error))]


def _pointer(path):
    return "/" + "/".join(str(part) for part in path)


def _scan(corpus):
    for directory in DOCUMENT_DIRS:
        base = corpus.root / directory
        if base.is_dir():
            corpus.files += [path.relative_to(corpus.root).as_posix() for path in base.rglob("*") if path.is_file()]
    corpus.files.sort()
    for path in corpus.files:
        if path.endswith(".json"):
            document, problems = read_document((corpus.root / path).read_bytes())
            corpus.documents[path] = document
            for code, detail in problems:
                corpus.report(path, code, detail)


def _load_schemas(corpus):
    validators = corpus.validators
    for path in corpus.files:
        if not path.startswith("schemas/"):
            continue
        match = SCHEMA_FILE.fullmatch(path[len("schemas/"):])
        if match is None:
            corpus.report(path, "unexpected-file", "schema files are schemas/<kind>-<version>.schema.json")
            continue
        schema = corpus.documents[path]
        if schema is None:
            continue
        urn = SCHEMA_URN.format(kind=match.group(1), version=match.group(2))
        if not isinstance(schema, dict) or schema.get("$id") != urn:
            corpus.report(path, "schema-id-mismatch", f"$id is not {urn}")
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as error:
            corpus.report(path, "schema-invalid", error.message)
            continue
        validators[urn] = check_contract.Validator(schema)
    for kind, version in sorted(SUPPORTED.items()):
        if SCHEMA_URN.format(kind=kind, version=version) not in validators and \
                f"schemas/{kind}-{version}.schema.json" not in corpus.files:
            corpus.report(f"schemas/{kind}-{version}.schema.json", "file-missing")


def _check_set(corpus, name, directory, log_path):
    namespaces = {namespace["id"]: namespace for namespace in corpus.registry["namespaces"]}
    cases, digests = {}, {}
    for path in corpus.files:
        if not path.startswith(directory + "/"):
            continue
        if not path.endswith(".json"):
            corpus.report(path, "unexpected-file", f"{directory} holds only case documents")
            continue
        case = corpus.document(path, "case")
        if case is None:
            continue
        cases[path] = case
        for code, detail in check_contract.check_case(case, corpus.registry):
            corpus.report(path, code, detail)
        parsed = check_contract.parse_id(case["id"])
        if parsed is None:
            continue
        namespace = namespaces.get(parsed[0])
        if namespace is not None and namespace["purpose"] != PURPOSES[name]:
            corpus.report(path, "case-set-mismatch", f"namespace {parsed[0]} has the purpose {namespace['purpose']}")
        expected = CONFORMANCE_PATH.format(namespace=parsed[0], id=case["id"])
        if name == "conformance" and path != expected:
            corpus.report(path, "case-path", f"{case['id']} belongs at {expected}")
        try:
            digests[path] = canonical.digest(case)
        except canonical.CanonicalError as error:
            corpus.report(path, "not-canonicalizable", str(error))

    paths_by_id = {}
    for path, case in cases.items():
        paths_by_id.setdefault(case["id"], []).append(path)
    for case_id, paths in paths_by_id.items():
        for path in paths if len(paths) > 1 else ():
            corpus.report(path, "duplicate-id", f"{case_id} is also in {', '.join(p for p in paths if p != path)}")
    for path, case in cases.items():
        for related in case["informative"].get("related", ()):
            if related not in paths_by_id:
                corpus.report(path, "related-unknown", related)

    log = corpus.document(log_path, "lifecycle")
    projection = None
    if log is not None:
        projection, problems = check_lifecycle.replay(log, [cases[path] for path in sorted(digests)])
        for code, detail in problems:
            # Duplicate IDs are reported on the case files.
            if code != "duplicate-id":
                corpus.report(log_path, code, detail)
    corpus.sets[name] = CaseSet(name, cases, digests, log, projection)


def _check_release_records(corpus):
    versions = {}
    for path in corpus.files:
        if path.startswith(RELEASES + "/"):
            parts = path.split("/")
            if len(parts) != 3:
                corpus.report(path, "unexpected-file", "release records are releases/<version>/<file>")
                continue
            versions.setdefault(parts[1], []).append(parts[2])
    for version, names in sorted(versions.items()):
        directory = f"{RELEASES}/{version}"
        if VERSION.fullmatch(version) is None:
            corpus.report(directory, "release-version-invalid", "not a Semantic Versioning 2.0.0 version")
            continue
        for name in sorted(set(names) - {RELEASE_MANIFEST, RELEASE_SUMS}):
            corpus.report(f"{directory}/{name}", "unexpected-file", f"a release record holds {RELEASE_MANIFEST} and "
                          f"{RELEASE_SUMS}")
        manifest = corpus.document(f"{directory}/{RELEASE_MANIFEST}", "manifest")
        if manifest is not None and manifest["release"] != version:
            corpus.report(f"{directory}/{RELEASE_MANIFEST}", "release-record-mismatch",
                          f"release {manifest['release']} recorded under {version}")
        sums_path = f"{directory}/{RELEASE_SUMS}"
        if RELEASE_SUMS not in names:
            corpus.report(sums_path, "file-missing")
            continue
        sums = parse_sums((corpus.root / sums_path).read_bytes())
        if sums is None:
            corpus.report(sums_path, "checksums-invalid", "lines are '<sha256>  <name>' in ASCII")
            continue
        base = archive_base(version)
        expected = {f"{base}.tar", f"{base}.manifest.json"}
        if set(sums) != expected:
            corpus.report(sums_path, "release-record-incomplete", f"lists {', '.join(sorted(sums))}, not "
                          f"{', '.join(sorted(expected))}")
        recorded = sums.get(f"{base}.manifest.json")
        if RELEASE_MANIFEST in names and recorded is not None and \
                recorded != sha256((corpus.root / directory / RELEASE_MANIFEST).read_bytes()):
            corpus.report(sums_path, "release-record-mismatch", f"{base}.manifest.json is not {RELEASE_MANIFEST}")


def validate(root=ROOT):
    """Validate the corpus tree at root and return the Corpus with its diagnostics."""
    corpus = Corpus(root)
    _scan(corpus)
    if (corpus.root / LICENSE).is_file():
        for code, detail in read_text((corpus.root / LICENSE).read_bytes())[1]:
            corpus.report(LICENSE, code, detail)
    else:
        corpus.report(LICENSE, "file-missing")
    _load_schemas(corpus)
    corpus.registry = corpus.document(REGISTRY, "registry")
    if corpus.registry is None:
        return corpus
    for code, detail in check_contract.check_registry(corpus.registry):
        corpus.report(REGISTRY, code, detail)
    for name, paths in check_lifecycle.SETS.items():
        present = paths["log"] in corpus.files or any(path.startswith(paths["cases"] + "/") for path in corpus.files)
        if present or name in REQUIRED_SETS:
            _check_set(corpus, name, paths["cases"], paths["log"])
    _check_release_records(corpus)
    return corpus


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("root", nargs="?", default=str(ROOT), help="corpus root, the repository by default")
    args = parser.parse_args(argv)
    corpus = validate(args.root)
    for diagnostic in corpus.diagnostics:
        print(diagnostic, file=sys.stderr)
    if corpus.diagnostics:
        print(f"corpus invalid: {len(corpus.diagnostics)} diagnostics", file=sys.stderr)
        return 1
    counts = ", ".join(f"{len(case_set.cases)} {PURPOSES[name]} cases"
                       for name, case_set in sorted(corpus.sets.items()))
    releases = len({path.split("/")[1] for path in corpus.files if path.startswith(RELEASES + "/")})
    print(f"corpus ok: {counts}, {releases} release records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
