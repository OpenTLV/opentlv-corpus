# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

from corpus_fixture import REVISION, case_path, read_json, write_corpus, write_json
from scripts import canonical, check_history, check_lifecycle, release, validate

VERSION = "0.1.0"
BASE = validate.archive_base(VERSION)
ARCHIVE = f"{BASE}.tar"
MANIFEST = f"{BASE}.manifest.json"


def lines(diagnostics):
    return [str(diagnostic) for diagnostic in diagnostics]


class ReleaseTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root = write_corpus(self.directory / "corpus")

    def build(self, output="out", root=None, version=VERSION, **options):
        options.setdefault("revision", REVISION)
        release.build(root or self.root, version, self.directory / output, **options)
        return self.directory / output

    def artifacts(self, output):
        return {path.name: path.read_bytes() for path in sorted(Path(output).iterdir())}


class BuildTest(ReleaseTestCase):
    def test_artifacts(self):
        output = self.build()
        artifacts = self.artifacts(output)
        self.assertEqual(sorted(artifacts), [release.SUMS, MANIFEST, ARCHIVE])
        self.assertEqual(validate.parse_sums(artifacts[release.SUMS]),
                         {ARCHIVE: validate.sha256(artifacts[ARCHIVE]), MANIFEST: validate.sha256(artifacts[MANIFEST])})
        top, files, problems = release.read_archive(artifacts[ARCHIVE])
        self.assertEqual((top, problems), (BASE, []))
        self.assertEqual(files[release.MANIFEST], artifacts[MANIFEST])

    def test_contents(self):
        _, files, _ = release.read_archive((self.build() / ARCHIVE).read_bytes())
        cases = [path for path in files if path.startswith("cases/")]
        self.assertEqual(len(cases), 11)
        self.assertEqual(sorted(path for path in files if not path.startswith("cases/")), [
            "LICENSE", "SHA256SUMS", "lifecycle-projection.json", "manifest.json", "registry/lifecycle.json",
            "registry/registry.json", "schemas/case-1.schema.json", "schemas/lifecycle-1.schema.json",
            "schemas/manifest-1.schema.json", "schemas/plan-1.schema.json", "schemas/registry-1.schema.json",
            "schemas/resolution-1.schema.json"])
        for path in cases + ["registry/registry.json", "LICENSE"]:
            self.assertEqual(files[path], (self.root / path).read_bytes())

    def test_manifest(self):
        _, files, _ = release.read_archive((self.build() / ARCHIVE).read_bytes())
        manifest = json.loads(files["manifest.json"])
        self.assertEqual(validate.validate(self.root).validators["urn:opentlv:corpus:schema:manifest:1"]
                         .is_valid(manifest), True)
        self.assertEqual((manifest["release"], manifest["phase"], manifest["source"]["revision"], manifest["tools"]),
                         (VERSION, "0.x", REVISION, release.TOOLS))
        self.assertEqual(manifest["licenses"], ["MIT"])
        self.assertEqual([schema["id"] for schema in manifest["schemas"]],
                         [f"urn:opentlv:corpus:schema:{kind}:1"
                          for kind in ("case", "lifecycle", "manifest", "plan", "registry", "resolution")])
        self.assertEqual(manifest["cases"], sorted(manifest["cases"], key=lambda case: case["id"]))
        for case in manifest["cases"]:
            self.assertEqual(case["path"], case_path(case["id"]))
            self.assertEqual(case["digest"], canonical.digest(read_json(self.root / case["path"])))
        listed = {entry["path"]: entry for entry in manifest["files"]}
        self.assertEqual(sorted(listed), sorted(set(files) - {"manifest.json", "SHA256SUMS"}))
        for path, entry in listed.items():
            self.assertEqual((entry["size"], entry["sha256"]), (len(files[path]), validate.sha256(files[path])))
        self.assertEqual(validate.parse_sums(files["SHA256SUMS"]),
                         {path: validate.sha256(data) for path, data in files.items() if path != "SHA256SUMS"})

    def test_projection_is_the_lifecycle_snapshot(self):
        _, files, _ = release.read_archive((self.build() / ARCHIVE).read_bytes())
        log, cases = check_lifecycle.load_set("conformance", self.root)
        self.assertEqual(json.loads(files[release.PROJECTION]), check_lifecycle.replay(log, cases)[0])

    def test_examples_and_release_records_are_not_shipped(self):
        shutil.copytree(validate.ROOT / "examples", self.root / "examples")
        _, files, _ = release.read_archive((self.build() / ARCHIVE).read_bytes())
        self.assertFalse([path for path in files if path.startswith(("examples/", "releases/"))])

    def test_members_are_normalized(self):
        data = (self.build() / ARCHIVE).read_bytes()
        self.assertEqual(data[257:265], b"ustar\x0000")
        self.assertEqual(len(data) % tarfile.RECORDSIZE, 0)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
        names = [member.name for member in members]
        self.assertEqual(names, sorted(names))
        for member in members:
            self.assertTrue(member.isreg())
            self.assertEqual(member.pax_headers, {})
            self.assertTrue(member.name.startswith(BASE + "/"))
            self.assertEqual((member.mtime, member.mode, member.uid, member.gid, member.uname, member.gname),
                             (0, 0o644, 0, 0, "", ""))


class ReproducibilityTest(ReleaseTestCase):
    def test_identical_input_gives_identical_bytes(self):
        self.assertEqual(self.artifacts(self.build("first")), self.artifacts(self.build("second")))

    def test_file_order_times_and_permissions_do_not_matter(self):
        copy = self.directory / "copy"
        for index, path in enumerate(sorted(self.root.rglob("*"), reverse=True)):
            if path.is_file():
                target = copy / path.relative_to(self.root)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
                os.utime(target, (1_000_000_000 + index, 1_000_000_000 + index))
                os.chmod(target, 0o600)
        self.assertEqual(self.artifacts(self.build("first")), self.artifacts(self.build("second", root=copy)))

    def test_build_checks_a_published_record(self):
        first = self.build("first")
        self.build("second", check=first / release.SUMS)
        (self.directory / "tampered").write_bytes((first / release.SUMS).read_bytes().replace(b"0", b"1", 1))
        with self.assertRaisesRegex(release.ReleaseError, "does not reproduce"):
            self.build("third", check=self.directory / "tampered")
        self.assertFalse((self.directory / "third").exists())

    def test_inputs_change_the_bytes(self):
        first = self.artifacts(self.build("first"))
        self.assertNotEqual(self.artifacts(self.build("second", version="0.1.1")), first)
        self.assertNotEqual(self.artifacts(self.build("third", revision="f" * 40)), first)


class RefusalTest(ReleaseTestCase):
    def test_invalid_corpus(self):
        write_json(self.root / "cases" / "asn1.der" / "copy.json", read_json(self.root / case_path("asn1.der.000006")))
        with self.assertRaisesRegex(release.ReleaseError, "cases/asn1.der/copy.json: case-path"):
            self.build()

    def test_versions(self):
        for version in ("1.0", "v0.1.0", "0.1.0+build.1", "01.0.0", "1.0.0"):
            with self.subTest(version):
                with self.assertRaises(release.ReleaseError):
                    self.build(version=version)
        self.build(version="0.2.0-rc.1")
        log_path = self.root / "registry" / "lifecycle.json"
        write_json(log_path, dict(read_json(log_path), phase="stable"))
        with self.assertRaisesRegex(release.ReleaseError, "does not match the stable phase"):
            self.build(version="0.3.0")
        self.build(version="1.0.0")

    def test_published_version(self):
        (self.root / "releases" / VERSION).mkdir(parents=True)
        with self.assertRaisesRegex(release.ReleaseError, "already published"):
            self.build()

    def test_revision(self):
        with self.assertRaisesRegex(release.ReleaseError, "not a full Git commit ID"):
            self.build(revision="b64666f")
        with self.assertRaisesRegex(release.ReleaseError, "not the top of a Git checkout"):
            self.build(revision=None)


@unittest.skipUnless(shutil.which("git"), "needs Git")
class SourceRevisionTest(ReleaseTestCase):
    def setUp(self):
        super().setUp()
        for args in (["init", "-q"], ["config", "core.autocrlf", "false"], ["config", "user.name", "Corpus Test"],
                     ["config", "user.email", "corpus-test@example.invalid"], ["add", "-A"],
                     ["commit", "-q", "-m", "corpus"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, check=True, capture_output=True,
                                   text=True).stdout.strip()

    def test_head_of_a_clean_checkout(self):
        self.assertEqual(release.source_revision(self.root), self.head)
        output = self.build(revision=None)
        self.assertEqual(json.loads((output / MANIFEST).read_bytes())["source"]["revision"], self.head)

    def test_inputs_must_be_the_committed_bytes(self):
        (self.root / "LICENSE").write_bytes((self.root / "LICENSE").read_bytes().replace(b"\n", b"\r\n"))
        with self.assertRaisesRegex(release.ReleaseError, r"differ from HEAD: LICENSE;"):
            release.source_revision(self.root)
        subprocess.run(["git", "checkout", "--", "LICENSE"], cwd=self.root, check=True, capture_output=True)
        shutil.copyfile(self.root / case_path("asn1.der.000006"), self.root / "cases" / "asn1.der" / "copy.json")
        (self.root / case_path("asn1.ber.000001")).unlink()
        with self.assertRaisesRegex(release.ReleaseError, rf"differ from HEAD: {case_path('asn1.ber.000001')}, "
                                                          r"cases/asn1.der/copy.json;"):
            release.source_revision(self.root)

    def test_files_outside_the_inputs_do_not_matter(self):
        (self.root / "README.md").write_text("draft\n", encoding="utf-8")
        self.assertEqual(release.source_revision(self.root), self.head)


class VerifyTest(ReleaseTestCase):
    def setUp(self):
        super().setUp()
        self.output = self.build()
        self.top, self.files, _ = release.read_archive((self.output / ARCHIVE).read_bytes())

    def verify(self, files=None, top=None, sums=False):
        path = self.directory / ARCHIVE
        path.write_bytes(release.write_archive(top or self.top, self.files if files is None else files))
        return lines(release.verify(path, self.output / release.SUMS if sums else None))

    def edited(self, path, change, resum=False):
        files = dict(self.files)
        document = json.loads(files[path])
        change(document)
        files[path] = release.json_bytes(document)
        if resum:
            files["SHA256SUMS"] = validate.format_sums({name: validate.sha256(data) for name, data in files.items()
                                                        if name != "SHA256SUMS"})
        return files

    def test_built_release_verifies(self):
        self.assertEqual(lines(release.verify(self.output / ARCHIVE, self.output / release.SUMS)), [])
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = release.main(["verify", str(self.output / ARCHIVE), "--sums", str(self.output / release.SUMS)])
        self.assertEqual((status, output.getvalue(), errors.getvalue()), (0, f"release ok: {ARCHIVE}\n", ""))

    def test_published_checksum(self):
        files = self.edited("registry/registry.json", lambda registry: registry["capabilities"].pop())
        found = self.verify(files, sums=True)
        actual = validate.sha256((self.directory / ARCHIVE).read_bytes())
        published = validate.parse_sums((self.output / release.SUMS).read_bytes())[ARCHIVE]
        self.assertIn(f"{ARCHIVE}: checksum-mismatch: SHA-256 is {actual}, SHA256SUMS has {published}", found)
        self.assertIn("registry/registry.json: manifest-file-mismatch", "\n".join(found))

    def test_tampered_definition(self):
        path = case_path("asn1.der.000007")
        files = self.edited(path, lambda case: case["normative"]["input"].update(octets="0101ff"), resum=True)
        digest = canonical.digest(json.loads(files[path]))
        listed = next(case["digest"] for case in json.loads(self.files["manifest.json"])["cases"]
                      if case["path"] == path)
        size, sha = len(files[path]), validate.sha256(files[path])
        old_size, old_sha = len(self.files[path]), validate.sha256(self.files[path])
        self.assertEqual(self.verify(files), sorted([
            f"{path}: manifest-file-mismatch: listed ({old_size}, '{old_sha}'), found ({size}, '{sha}')",
            f"asn1.der.000007: manifest-case-mismatch: listed ('{path}', '{listed}'), found ('{path}', '{digest}')",
            "lifecycle-projection.json: projection-mismatch: differs from the replay of registry/lifecycle.json",
            "registry/lifecycle.json: event-digest-mismatch: event 2: asn1.der.000007",
        ]))

    def test_tampered_manifest_digest(self):
        files = self.edited("manifest.json", lambda manifest: manifest["cases"][0].update(digest="sha256:" + "0" * 64),
                            resum=True)
        case = json.loads(self.files["manifest.json"])["cases"][0]
        self.assertEqual(self.verify(files), [
            f"{case['id']}: manifest-case-mismatch: listed ('{case['path']}', 'sha256:{'0' * 64}'), "
            f"found ('{case['path']}', '{case['digest']}')"])

    def test_incomplete_manifest(self):
        files = self.edited("manifest.json", lambda manifest: manifest["files"].pop(), resum=True)
        last = json.loads(self.files["manifest.json"])["files"][-1]["path"]
        self.assertEqual(self.verify(files), [f"{last}: manifest-file-unlisted"])
        files = self.edited("manifest.json", lambda manifest: manifest["cases"].pop(), resum=True)
        self.assertEqual(self.verify(files), ["asn1.der.000009: manifest-case-unlisted"])

    def test_files_missing_from_the_archive(self):
        files = dict(self.files)
        del files["schemas/case-1.schema.json"]
        found = self.verify(files)
        self.assertIn("schemas/case-1.schema.json: manifest-file-missing", found)
        self.assertIn("schemas/case-1.schema.json: checksums-missing", found)
        self.assertIn("schemas/case-1.schema.json: file-missing", found)

    def test_files_outside_the_contract(self):
        files = dict(self.files, **{"README.md": b"x\n"})
        self.assertEqual(self.verify(files), ["README.md: checksums-unlisted", "README.md: manifest-file-unlisted",
                                              "README.md: unexpected-file: not part of the data release contract"])

    def test_damaged_schema_file(self):
        files = dict(self.files, **{"schemas/case-1.schema.json": b"[]\n"})
        found = self.verify(files)
        self.assertIn("schemas/case-1.schema.json: schema-id-mismatch: $id is not urn:opentlv:corpus:schema:case:1",
                      found)
        self.assertIn("manifest.json: manifest-schemas-mismatch", "\n".join(found))

    def test_inner_checksums(self):
        files = dict(self.files, SHA256SUMS=self.files["SHA256SUMS"].replace(b"  LICENSE\n", b"  LICENSE.txt\n"))
        self.assertEqual(self.verify(files), ["LICENSE: checksums-unlisted", "LICENSE.txt: checksums-missing"])

    def test_release_identity(self):
        self.assertEqual(self.verify(top=validate.archive_base("9.9.9")),
                         [f"manifest.json: manifest-release-mismatch: release {VERSION} in directory "
                          f"{validate.archive_base('9.9.9')}"])

    def test_archive_structure(self):
        path = self.directory / "bad.tar"
        path.write_bytes(b"not an archive")
        self.assertEqual([item.code for item in release.verify(path)], ["archive-invalid"])
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(self.files.items()):
                info = tarfile.TarInfo(f"{self.top}/{name}")
                info.size, info.mtime = len(data), 1700000000
                archive.addfile(info, io.BytesIO(data))
            evil = tarfile.TarInfo(f"{self.top}/../evil")
            archive.addfile(evil, io.BytesIO(b""))
            link = tarfile.TarInfo(f"{self.top}/link")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            archive.addfile(link)
        path.write_bytes(buffer.getvalue())
        found = lines(release.verify(path))
        self.assertIn(f"{self.top}/../evil: archive-member-path: members are files below one directory", found)
        self.assertIn("link: archive-member-type: members are regular files", found)
        self.assertIn("LICENSE: archive-member-metadata: mtime, mode, owner or group differ from the release contract",
                      found)
        self.assertFalse((self.directory / "evil").exists())

    def test_member_order(self):
        path = self.directory / "unordered.tar"
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name in sorted(self.files, reverse=True):
                info = tarfile.TarInfo(f"{self.top}/{name}")
                info.size, info.mtime, info.mode, info.uname, info.gname = len(self.files[name]), 0, 0o644, "", ""
                archive.addfile(info, io.BytesIO(self.files[name]))
        path.write_bytes(buffer.getvalue())
        self.assertEqual(lines(release.verify(path)), ["unordered.tar: archive-order: members are ordered by path"])


class RecordTest(ReleaseTestCase):
    def setUp(self):
        super().setUp()
        output = self.build()
        self.record = self.root / "releases" / VERSION
        self.record.mkdir(parents=True)
        shutil.copyfile(output / MANIFEST, self.record / "manifest.json")
        shutil.copyfile(output / release.SUMS, self.record / "SHA256SUMS")

    def diagnostics(self):
        return [str(item) for item in validate.validate(self.root).diagnostics]

    def test_published_record_is_valid(self):
        self.assertEqual(self.diagnostics(), [])

    def test_record_must_match(self):
        write_json(self.record / "manifest.json", dict(read_json(self.record / "manifest.json"), licenses=[]))
        self.assertEqual(self.diagnostics(), [f"releases/{VERSION}/SHA256SUMS: release-record-mismatch: "
                                              f"{MANIFEST} is not manifest.json"])

    def test_record_under_another_version(self):
        self.record.rename(self.root / "releases" / "0.2.0")
        base = validate.archive_base("0.2.0")
        self.assertEqual(self.diagnostics(), [
            f"releases/0.2.0/SHA256SUMS: release-record-incomplete: lists {MANIFEST}, {ARCHIVE}, "
            f"not {base}.manifest.json, {base}.tar",
            f"releases/0.2.0/manifest.json: release-record-mismatch: release {VERSION} recorded under 0.2.0"])

    def test_record_layout(self):
        (self.record / "notes.txt").write_text("x", encoding="utf-8")
        (self.root / "releases" / "latest").mkdir()
        (self.root / "releases" / "latest" / "SHA256SUMS").write_bytes(b"")
        (self.root / "releases" / "0.0.1").mkdir()
        (self.root / "releases" / "0.0.1" / "SHA256SUMS").write_bytes(b"not sums\n")
        self.assertEqual(self.diagnostics(), [
            "releases/0.0.1/SHA256SUMS: checksums-invalid: lines are '<sha256>  <name>' in ASCII",
            "releases/0.0.1/manifest.json: file-missing",
            f"releases/{VERSION}/notes.txt: unexpected-file: a release record holds manifest.json and SHA256SUMS",
            "releases/latest: release-version-invalid: not a Semantic Versioning 2.0.0 version"])


class HistoryTest(unittest.TestCase):
    class Base:
        def __init__(self, files):
            self._files = files

        def files(self, directory):
            return sorted(path for path in self._files if path.startswith(directory + "/"))

        def read(self, path):
            return self._files.get(path)

    def test_published_records_never_change(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory, "releases", VERSION)
            record.mkdir(parents=True)
            (record / "SHA256SUMS").write_bytes(b"a")
            base = self.Base({f"releases/{VERSION}/SHA256SUMS": b"a", f"releases/{VERSION}/manifest.json": b"{}"})
            self.assertEqual(check_history.check_release_records(base, directory),
                             [f"releases/{VERSION}/manifest.json: release-record-removed"])
            (record / "manifest.json").write_bytes(b"{}")
            (record / "SHA256SUMS").write_bytes(b"b")
            self.assertEqual(check_history.check_release_records(base, directory),
                             [f"releases/{VERSION}/SHA256SUMS: release-record-changed"])
            (record / "SHA256SUMS").write_bytes(b"a")
            Path(directory, "releases", "0.2.0").mkdir()
            Path(directory, "releases", "0.2.0", "SHA256SUMS").write_bytes(b"new")
            self.assertEqual(check_history.check_release_records(base, directory), [])


# Reads a release with the standard library and jsonschema only; the corpus
# tools are not importable in an isolated interpreter.
READER = r"""
import hashlib, json, sys, tarfile
from jsonschema import Draft202012Validator
try:
    import scripts.validate
    sys.exit("corpus tools are importable")
except ImportError:
    pass
with tarfile.open(sys.argv[1]) as archive:
    files = {member.name.split("/", 1)[1]: archive.extractfile(member).read() for member in archive}
for line in files["SHA256SUMS"].decode("ascii").splitlines():
    digest, name = line.split("  ", 1)
    assert hashlib.sha256(files[name]).hexdigest() == digest, name
schemas = {}
for name, data in files.items():
    if name.startswith("schemas/"):
        schema = json.loads(data)
        schemas[schema["$id"]] = Draft202012Validator(schema)
manifest = json.loads(files["manifest.json"])
schemas[manifest["schema"]].validate(manifest)
for case in manifest["cases"]:
    document = json.loads(files[case["path"]])
    schemas[document["schema"]].validate(document)
    assert document["id"] == case["id"]
statuses = {case["id"]: case["status"] for case in json.loads(files["lifecycle-projection.json"])["cases"]}
print(len(manifest["cases"]), statuses["asn1.der.000007"])
"""


class DataOnlyTest(ReleaseTestCase):
    def test_readable_without_the_corpus_tools(self):
        archive = self.build() / ARCHIVE
        result = subprocess.run([sys.executable, "-I", "-c", READER, str(archive)], cwd=self.directory,
                                capture_output=True, text=True, check=False)
        self.assertEqual((result.returncode, result.stderr, result.stdout), (0, "", "11 invalid\n"))

    @unittest.skipUnless(shutil.which("tar") and shutil.which("sha256sum"), "needs tar and sha256sum")
    def test_verifiable_with_standard_tools(self):
        output = self.build()
        subprocess.run(["sha256sum", "--check", "--strict", release.SUMS], cwd=output, check=True, capture_output=True)
        subprocess.run(["tar", "-xf", ARCHIVE], cwd=output, check=True, capture_output=True)
        subprocess.run(["sha256sum", "--check", "--strict", "SHA256SUMS"], cwd=output / BASE, check=True,
                       capture_output=True)


class DependencyTest(unittest.TestCase):
    def test_tools_depend_only_on_the_standard_library_and_jsonschema(self):
        third_party = set()
        for path in sorted((validate.ROOT / "scripts").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else \
                    [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                third_party |= {name.split(".")[0] for name in names} - set(sys.stdlib_module_names) - {"scripts"}
        self.assertEqual(third_party, {"jsonschema"})
        requirements = (validate.ROOT / "requirements-dev.txt").read_text(encoding="utf-8").split()
        self.assertEqual([line.split("==")[0] for line in requirements], ["jsonschema"])


if __name__ == "__main__":
    unittest.main()
