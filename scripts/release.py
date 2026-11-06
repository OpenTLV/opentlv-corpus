# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Build and verify the data-only release of the corpus.

A data release is one uncompressed POSIX ustar archive with the conformance
cases, the registry, the lifecycle log and its projection, the schema files
and the license, plus a manifest and SHA-256 sums. It is built only from a
corpus without diagnostics, it can be read without Python, and it is
reproducible: the same files at the same revision give the same archive bytes
on every platform. See docs/release.md.
"""

import argparse
from collections import namedtuple
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import check_contract, validate  # noqa: E402
from scripts.validate import Diagnostic, archive_base, format_sums, parse_sums, sha256  # noqa: E402

ROOT = validate.ROOT
CORPUS = "opentlv-corpus"
REPOSITORY = "https://github.com/OpenTLV/opentlv-corpus"
TOOLS = {"name": "opentlv-corpus-tools", "version": "0.1.0-dev"}
MANIFEST = "manifest.json"
SUMS = "SHA256SUMS"
PROJECTION = "lifecycle-projection.json"
REGISTRY_FILES = ("registry/registry.json", "registry/lifecycle.json")
DATA_DIRS = ("schemas/", "cases/")
INPUTS = ("LICENSE", "schemas", "registry", "cases")
REVISION = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")
# Every member is a plain file with the same owner, mode and time, so the
# archive depends only on the paths and contents.
MEMBER = {"mtime": 0, "mode": 0o644, "uid": 0, "gid": 0, "uname": "", "gname": ""}


class ReleaseError(Exception):
    """The release cannot be built."""


def json_bytes(document):
    """Serialize a generated document as indented UTF-8 JSON with a final line feed."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def check_version(version, phase):
    """Raise ReleaseError unless the version is valid and matches the lifecycle phase."""
    if validate.VERSION.fullmatch(version) is None:
        raise ReleaseError(f"{version!r} is not a Semantic Versioning 2.0.0 version without build metadata")
    if (phase == "stable") != (int(version.split(".")[0]) >= 1):
        raise ReleaseError(f"release {version} does not match the {phase} phase: 0.x releases are 0.y.z")


def _licenses(case):
    expressions = [case["informative"]["provenance"]["license"]]
    expressions += [vector["license"] for vector in case["informative"]["oracle"].get("vectors", ())]
    return {license for expression in expressions for license in check_contract.license_ids(expression) or ()}


def _schema_id(schema):
    return schema.get("$id") if isinstance(schema, dict) else None


def release_files(corpus, version, revision, tools=TOOLS):
    """Return {path: octets} of the data release of a validated corpus."""
    files = {path: (corpus.root / path).read_bytes() for path in corpus.files
             if path.startswith(DATA_DIRS) or path in REGISTRY_FILES}
    license_path = corpus.root / validate.LICENSE
    files[validate.LICENSE] = license_path.read_bytes() if license_path.is_file() else b""
    conformance = corpus.sets["conformance"]
    files[PROJECTION] = json_bytes(conformance.projection)
    cases = sorted(({"id": conformance.cases[path]["id"], "path": path, "digest": digest}
                    for path, digest in conformance.digests.items()), key=lambda case: (case["id"], case["path"]))
    schemas = [{"id": _schema_id(corpus.documents.get(path)), "path": path} for path in sorted(files)
               if path.startswith("schemas/")]
    manifest = {
        "schema": validate.SCHEMA_URN.format(kind="manifest", version=validate.SUPPORTED["manifest"]),
        "corpus": CORPUS,
        "release": version,
        "phase": conformance.log["phase"],
        "source": {"repository": REPOSITORY, "revision": revision},
        "tools": {"name": tools["name"], "version": tools["version"]},
        "schemas": schemas,
        "licenses": sorted(set().union(*(_licenses(case) for case in conformance.cases.values()))),
        "cases": cases,
        "files": [{"path": path, "size": len(data), "sha256": sha256(data)} for path, data in sorted(files.items())],
    }
    files[MANIFEST] = json_bytes(manifest)
    files[SUMS] = format_sums({path: sha256(data) for path, data in files.items()})
    return files


def write_archive(top, files):
    """Return the ustar archive of {path: octets} under the directory top, members ordered by path."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT, encoding="ascii") as archive:
        for path in sorted(files):
            info = tarfile.TarInfo(f"{top}/{path}")
            info.size = len(files[path])
            for name, value in MEMBER.items():
                setattr(info, name, value)
            archive.addfile(info, io.BytesIO(files[path]))
    return buffer.getvalue()


def read_archive(data, label="archive"):
    """Return the top directory, {path: octets} and diagnostics of archive octets.

    Only regular files below one top directory are read; other members and
    unsafe paths are reported and skipped.
    """
    problems, files, names, top = set(), {}, [], None
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for info in archive:
                names.append(info.name)
                parts = info.name.split("/")
                if len(parts) < 2 or "\\" in info.name or any(part in ("", ".", "..") for part in parts) or \
                        (top is not None and parts[0] != top):
                    problems.add(Diagnostic(info.name, "archive-member-path", "members are files below one directory"))
                    continue
                top = parts[0]
                path = "/".join(parts[1:])
                if not info.isreg():
                    problems.add(Diagnostic(path, "archive-member-type", "members are regular files"))
                    continue
                if any(getattr(info, name) != value for name, value in MEMBER.items()):
                    problems.add(Diagnostic(path, "archive-member-metadata", "mtime, mode, owner or group differ "
                                            "from the release contract"))
                if path in files:
                    problems.add(Diagnostic(path, "archive-member-duplicate", ""))
                files[path] = archive.extractfile(info).read()
    except tarfile.TarError as error:
        return None, {}, [Diagnostic(label, "archive-invalid", str(error))]
    if names != sorted(names):
        problems.add(Diagnostic(label, "archive-order", "members are ordered by path"))
    if top is None:
        problems.add(Diagnostic(label, "archive-invalid", "the archive has no files"))
    return top, files, sorted(problems)


def _git(root, *args, stdin=None):
    return subprocess.run(["git", *args], cwd=root, input=stdin, capture_output=True, check=False)


def source_revision(root):
    """Return the commit checked out at root, refusing release inputs that differ from it.

    The working tree files are compared with the committed blobs byte for
    byte, without Git's line-ending conversion, so a checkout with CRLF line
    endings is refused instead of producing an archive that differs from a
    build on another platform.
    """
    root = Path(root)
    head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if head.returncode or toplevel.returncode or \
            Path(toplevel.stdout.decode("utf-8").strip()).resolve() != root.resolve():
        raise ReleaseError(f"{root} is not the top of a Git checkout; pass --revision")
    listed = _git(root, "ls-tree", "-r", "-z", "HEAD", "--", *INPUTS)
    committed = {}
    for entry in listed.stdout.decode("utf-8").split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            committed[path] = meta.split()[2]
    present = [validate.LICENSE] if (root / validate.LICENSE).is_file() else []
    for directory in INPUTS[1:]:
        if (root / directory).is_dir():
            present += [path.relative_to(root).as_posix() for path in (root / directory).rglob("*") if path.is_file()]
    present.sort()
    hashed = _git(root, "hash-object", "--no-filters", "--stdin-paths",
                  stdin="".join(f"{path}\n" for path in present).encode("utf-8"))
    current = dict(zip(present, hashed.stdout.decode("ascii").split()))
    differing = sorted(path for path in committed.keys() | current.keys() if committed.get(path) != current.get(path))
    if hashed.returncode or differing:
        raise ReleaseError(f"release inputs differ from HEAD: {', '.join(differing)}; commit them, check them out "
                           "again or pass --revision")
    return head.stdout.decode("ascii").strip()


def build(root, version, output, revision=None, check=None):
    """Build the release artifacts of a corpus into output and return their paths.

    check is an optional SHA256SUMS file, such as a published release record,
    that the built artifacts must reproduce.
    """
    root, output = Path(root), Path(output)
    if (root / validate.RELEASES / version).exists():
        raise ReleaseError(f"release {version} is already published in {validate.RELEASES}/{version}")
    corpus = validate.validate(root)
    if corpus.diagnostics:
        raise ReleaseError("the corpus is invalid:\n" + "\n".join(str(item) for item in corpus.diagnostics))
    check_version(version, corpus.sets["conformance"].log["phase"])
    revision = revision or source_revision(root)
    if REVISION.fullmatch(revision) is None:
        raise ReleaseError(f"{revision!r} is not a full Git commit ID")
    files = release_files(corpus, version, revision)
    base = archive_base(version)
    artifacts = {f"{base}.tar": write_archive(base, files), f"{base}.manifest.json": files[MANIFEST]}
    artifacts[SUMS] = format_sums({name: sha256(data) for name, data in artifacts.items()})
    if check is not None and Path(check).read_bytes() != artifacts[SUMS]:
        raise ReleaseError(f"release {version} at {revision} does not reproduce {check}")
    output.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        (output / name).write_bytes(data)
    return [output / name for name in sorted(artifacts)]


def _check_sums(archive_path, data, sums_path):
    sums_path = Path(sums_path)
    sums = parse_sums(sums_path.read_bytes())
    if sums is None:
        return [Diagnostic(sums_path.name, "checksums-invalid", "lines are '<sha256>  <name>' in ASCII")]
    problems = []
    if archive_path.name not in sums:
        problems.append(Diagnostic(sums_path.name, "checksum-missing", archive_path.name))
    for name, digest in sorted(sums.items()):
        if name == archive_path.name:
            actual = sha256(data)
        elif (sums_path.parent / name).is_file():
            actual = sha256((sums_path.parent / name).read_bytes())
        else:
            continue
        if actual != digest:
            problems.append(Diagnostic(name, "checksum-mismatch",
                                       f"SHA-256 is {actual}, {sums_path.name} has {digest}"))
    return problems


def _compare_listing(corpus, listed, actual, code):
    for path in sorted(actual.keys() - listed.keys()):
        corpus.report(path, f"{code}-unlisted")
    for path in sorted(listed.keys() - actual.keys()):
        corpus.report(path, f"{code}-missing")
    for path in sorted(listed.keys() & actual.keys()):
        if listed[path] != actual[path]:
            corpus.report(path, f"{code}-mismatch", f"listed {listed[path]}, found {actual[path]}")


def _check_contents(corpus, top, files):
    """Report where the release contents differ from the manifest and from what the data defines."""
    manifest = corpus.document(MANIFEST, "manifest")
    if manifest is None:
        return
    if top != archive_base(manifest["release"]):
        corpus.report(MANIFEST, "manifest-release-mismatch", f"release {manifest['release']} in directory {top}")
    try:
        check_version(manifest["release"], manifest["phase"])
    except ReleaseError as error:
        corpus.report(MANIFEST, "release-version-phase", str(error))
    for path in sorted(files):
        if path not in (MANIFEST, SUMS, PROJECTION, validate.LICENSE, *REGISTRY_FILES) and \
                not path.startswith(DATA_DIRS):
            corpus.report(path, "unexpected-file", "not part of the data release contract")

    entries = [entry["path"] for entry in manifest["files"]]
    if entries != sorted(set(entries)):
        corpus.report(MANIFEST, "manifest-order", "files are listed once each, ordered by path")
    _compare_listing(corpus, {entry["path"]: (entry["size"], entry["sha256"]) for entry in manifest["files"]},
                     {path: (len(data), sha256(data)) for path, data in files.items() if path not in (MANIFEST, SUMS)},
                     "manifest-file")
    if SUMS not in files:
        corpus.report(SUMS, "file-missing")
    else:
        sums = parse_sums(files[SUMS])
        if sums is None:
            corpus.report(SUMS, "checksums-invalid", "lines are '<sha256>  <name>' in ASCII")
        else:
            _compare_listing(corpus, sums, {path: sha256(data) for path, data in files.items() if path != SUMS},
                             "checksums")

    conformance = corpus.sets.get("conformance")
    if conformance is None or conformance.log is None:
        return
    expected = json.loads(release_files(corpus, manifest["release"], manifest["source"]["revision"],
                                        manifest["tools"])[MANIFEST])
    if manifest["phase"] != expected["phase"]:
        corpus.report(MANIFEST, "manifest-phase-mismatch",
                      f"manifest has {manifest['phase']}, the log {expected['phase']}")
    _compare_listing(corpus, {case["id"]: (case["path"], case["digest"]) for case in manifest["cases"]},
                     {case["id"]: (case["path"], case["digest"]) for case in expected["cases"]}, "manifest-case")
    for field in ("schemas", "licenses"):
        if manifest[field] != expected[field]:
            corpus.report(MANIFEST, f"manifest-{field}-mismatch", f"expected {json.dumps(expected[field])}")
    if files.get(PROJECTION) != json_bytes(conformance.projection):
        corpus.report(PROJECTION, "projection-mismatch", "differs from the replay of registry/lifecycle.json")


Release = namedtuple("Release", "release sha256 revision corpus")
Release.__doc__ = """A verified data release: its version, archive SHA-256, source revision and corpus.

corpus is the validated validate.Corpus of the release contents; its documents
and case sets stay in memory after the extracted files are gone.
"""


def open_release(archive_path, sums_path=None):
    """Verify a data release archive, and its SHA256SUMS if given.

    Return the Release, or None when the archive cannot be read, and the
    sorted diagnostics. A release with diagnostics must not be used.
    """
    archive_path = Path(archive_path)
    data = archive_path.read_bytes()
    problems = set(_check_sums(archive_path, data, sums_path)) if sums_path is not None else set()
    top, files, archive_problems = read_archive(data, archive_path.name)
    problems |= set(archive_problems)
    if top is None:
        return None, sorted(problems)
    with tempfile.TemporaryDirectory() as directory:
        for path, content in files.items():
            target = Path(directory, *path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        corpus = validate.validate(directory)
        _check_contents(corpus, top, files)
    problems |= set(corpus.diagnostics)
    manifest = corpus.documents.get(MANIFEST) if not corpus.diagnostics else None
    if manifest is None:
        return None, sorted(problems)
    return Release(manifest["release"], sha256(data), manifest["source"]["revision"], corpus), sorted(problems)


def verify(archive_path, sums_path=None):
    """Return the diagnostics of a data release archive, and of its SHA256SUMS if given."""
    return open_release(archive_path, sums_path)[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    build_parser = commands.add_parser("build", help="build the release artifacts")
    build_parser.add_argument("--version", required=True, help="data release version, such as 0.1.0")
    build_parser.add_argument("--root", default=str(ROOT), help="corpus root, the repository by default")
    build_parser.add_argument("--output", default=str(ROOT / "build" / "release"), help="output directory")
    build_parser.add_argument("--revision", help="source commit; by default HEAD, which must match the inputs")
    build_parser.add_argument("--check", help="SHA256SUMS that the artifacts must reproduce")
    verify_parser = commands.add_parser("verify", help="verify a release archive")
    verify_parser.add_argument("archive", help="the .tar archive")
    verify_parser.add_argument("--sums", help="the SHA256SUMS published with it")
    args = parser.parse_args(argv)
    if args.command == "build":
        try:
            paths = build(args.root, args.version, args.output, args.revision, args.check)
        except ReleaseError as error:
            print(error, file=sys.stderr)
            return 1
        for path in paths:
            print(f"{sha256(path.read_bytes())}  {path}")
        return 0
    problems = verify(args.archive, args.sums)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"release invalid: {len(problems)} diagnostics", file=sys.stderr)
        return 1
    print(f"release ok: {Path(args.archive).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
