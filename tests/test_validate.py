# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import contextlib
import io
from pathlib import Path
import shutil
import tempfile
import unittest

from corpus_fixture import case_path, read_json, write_corpus, write_json
from scripts import canonical, validate

# Cases no other case relates to and no lifecycle event names, so breaking one
# reports only its own diagnostics.
LONE = "asn1.der.000006"
VALUE = "asn1.der.000003"


def lines(root):
    return [str(diagnostic) for diagnostic in validate.validate(root).diagnostics]


class CorpusTestCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = write_corpus(Path(directory.name) / "corpus")

    def path(self, relative):
        return self.root / relative

    def edit(self, relative, change, ensure_ascii=False):
        document = read_json(self.path(relative))
        change(document)
        write_json(self.path(relative), document, ensure_ascii)

    def edit_bytes(self, relative, change):
        self.path(relative).write_bytes(change(self.path(relative).read_bytes()))

    def assertDiagnostics(self, expected):
        self.assertEqual(lines(self.root), expected)


class GoldenTest(CorpusTestCase):
    def test_repository_is_valid(self):
        corpus = validate.validate()
        self.assertEqual(corpus.diagnostics, [])
        self.assertEqual(len(corpus.sets["examples"].cases), 11)
        self.assertEqual(corpus.sets["conformance"].log["events"], [])

    def test_fixture_is_valid(self):
        corpus = validate.validate(self.root)
        self.assertEqual(corpus.diagnostics, [])
        self.assertEqual(set(corpus.sets), {"conformance"})
        conformance = corpus.sets["conformance"]
        self.assertEqual(len(conformance.cases), 11)
        self.assertEqual({path: canonical.digest(case) for path, case in conformance.cases.items()},
                         conformance.digests)
        statuses = {item["id"]: item["status"] for item in conformance.projection["cases"]}
        self.assertEqual((statuses["asn1.der.000007"], statuses["asn1.der.000009"]), ("invalid", "deprecated"))

    def test_command_line(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(validate.main([str(self.root)]), 0)
        self.assertEqual(output.getvalue(), "corpus ok: 11 conformance cases, 0 release records\n")
        self.path("LICENSE").unlink()
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(validate.main([str(self.root)]), 1)
        self.assertEqual(errors.getvalue(), "LICENSE: file-missing\ncorpus invalid: 1 diagnostics\n")


class DeterminismTest(CorpusTestCase):
    def test_diagnostics_are_sorted_and_repeatable(self):
        self.edit(case_path(LONE), lambda case: case["normative"]["input"]["tree"].update(number="x"))
        self.edit(case_path(VALUE), lambda case: case["informative"]["provenance"].update(license="GPL-3.0-only"))
        (self.path("cases") / "notes.txt").write_text("x", encoding="utf-8")
        first = validate.validate(self.root).diagnostics
        self.assertEqual(len(first), 3)
        self.assertEqual(first, sorted(first))
        self.assertEqual(validate.validate(self.root).diagnostics, first)

    def test_diagnostics_do_not_depend_on_file_creation_order(self):
        self.edit(case_path(LONE), lambda case: case.update(id="asn1.der.000003"))
        copy = Path(self.root.parent, "copy")
        for path in sorted(self.root.rglob("*"), reverse=True):
            target = copy / path.relative_to(self.root)
            if path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        self.assertEqual(lines(copy), lines(self.root))


class EncodingTest(CorpusTestCase):
    def test_byte_order_mark(self):
        self.edit_bytes(case_path(LONE), lambda data: b"\xef\xbb\xbf" + data)
        self.assertDiagnostics([f"{case_path(LONE)}: encoding-bom: a byte order mark precedes the text"])

    def test_carriage_returns(self):
        self.edit_bytes(case_path(LONE), lambda data: data.replace(b"\n", b"\r\n"))
        self.assertDiagnostics([f"{case_path(LONE)}: encoding-line-ending: a carriage return; lines end with LF only"])

    def test_final_newline(self):
        self.edit_bytes(case_path(LONE), lambda data: data.rstrip(b"\n"))
        self.assertDiagnostics([f"{case_path(LONE)}: encoding-final-newline: "
                                "the text does not end with a line feed"])

    def test_invalid_utf8(self):
        self.edit_bytes(case_path(LONE), lambda data: data.replace(b'"title": "', b'"title": "\xff', 1))
        diagnostics = lines(self.root)
        self.assertEqual(len(diagnostics), 1)
        self.assertRegex(diagnostics[0], rf"^{case_path(LONE)}: encoding-invalid: not UTF-8 at octet [0-9]+$")

    def test_ambiguous_json(self):
        self.edit_bytes(case_path(LONE), lambda data: data.replace(b'"title":', b'"title": "x", "title":', 1))
        self.assertDiagnostics([f"{case_path(LONE)}: json-invalid: duplicate member 'title'"])
        self.edit_bytes(case_path(LONE), lambda data: b"[NaN]\n")
        self.assertDiagnostics([f"{case_path(LONE)}: json-invalid: NaN is not JSON"])

    def test_every_document_directory_and_the_license_are_checked(self):
        self.edit_bytes("registry/lifecycle.json", lambda data: data.replace(b"\n", b"\r\n"))
        self.edit_bytes("schemas/case-1.schema.json", lambda data: data.rstrip(b"\n"))
        self.edit_bytes("LICENSE", lambda data: data.replace(b"\n", b"\r\n"))
        self.assertDiagnostics([
            "LICENSE: encoding-line-ending: a carriage return; lines end with LF only",
            "registry/lifecycle.json: encoding-line-ending: a carriage return; lines end with LF only",
            "schemas/case-1.schema.json: encoding-final-newline: the text does not end with a line feed",
        ])


class CaseTest(CorpusTestCase):
    def test_schema_errors_name_the_location_and_keyword(self):
        self.edit(case_path(LONE), lambda case: case["normative"]["input"]["tree"].update(number="x"))
        self.assertDiagnostics([f"{case_path(LONE)}: schema: /normative/input/tree/number (pattern)"])

    def test_unsupported_schema_version(self):
        self.edit(case_path(LONE), lambda case: case.update(schema="urn:opentlv:corpus:schema:case:2"))
        self.assertDiagnostics([f"{case_path(LONE)}: schema-unsupported: 'urn:opentlv:corpus:schema:case:2' is not "
                                "urn:opentlv:corpus:schema:case:1"])

    def test_contract_rules(self):
        self.edit(case_path(LONE), lambda case: case["informative"]["provenance"].update(license="GPL-3.0-only"))
        self.edit(case_path(VALUE), lambda case: case["normative"]["standard"].update(rules="ber"))
        self.assertDiagnostics([f"{case_path(VALUE)}: rules-mismatch: ber in asn1.der",
                                f"{case_path(LONE)}: license-unregistered: provenance: GPL-3.0-only"])

    def test_duplicate_ids(self):
        shutil.copyfile(self.path(case_path(LONE)), self.path("cases/asn1.der/copy.json"))
        self.assertDiagnostics([
            f"cases/asn1.der/asn1.der.000006.json: duplicate-id: {LONE} is also in cases/asn1.der/copy.json",
            f"cases/asn1.der/copy.json: case-path: {LONE} belongs at cases/asn1.der/asn1.der.000006.json",
            f"cases/asn1.der/copy.json: duplicate-id: {LONE} is also in cases/asn1.der/asn1.der.000006.json",
        ])

    def test_case_path(self):
        self.path(case_path(LONE)).rename(self.path("cases/asn1.ber/asn1.der.000006.json"))
        self.assertDiagnostics([f"cases/asn1.ber/asn1.der.000006.json: case-path: {LONE} belongs at "
                                f"{case_path(LONE)}"])

    def test_examples_are_never_conformance_cases(self):
        example = read_json(validate.ROOT / "examples" / "valid" / "encode-tree.json")
        write_json(self.path(case_path(example["id"])), example)
        self.assertDiagnostics([f"{case_path(example['id'])}: case-set-mismatch: namespace example.der has the "
                                "purpose example"])

    def test_related_cases_exist(self):
        self.edit(case_path(LONE), lambda case: case["informative"].update(related=["asn1.ber.000002",
                                                                                    "asn1.cer.000001"]))
        self.assertDiagnostics([f"{case_path(LONE)}: related-unknown: asn1.cer.000001"])

    def test_definitions_must_be_canonicalizable(self):
        def change(case):
            case["normative"]["type"] = {"kind": "bit_string"}
            case["normative"]["input"]["value"] = {"kind": "bit_string", "hex": "a0", "unused_bits": 5.0}

        self.edit(case_path(VALUE), change)
        self.assertDiagnostics([f"{case_path(VALUE)}: not-canonicalizable: /normative/input/value/unused_bits: "
                                "floating-point number 5.0"])

    def test_unexpected_files(self):
        (self.path("cases") / "notes.txt").write_text("x", encoding="utf-8")
        (self.path("schemas") / "README.md").write_text("x", encoding="utf-8")
        self.assertDiagnostics(["cases/notes.txt: unexpected-file: cases holds only case documents",
                                "schemas/README.md: unexpected-file: schema files are "
                                "schemas/<kind>-<version>.schema.json"])


class LifecycleTest(CorpusTestCase):
    def test_tampered_event_digest(self):
        self.edit("registry/lifecycle.json", lambda log: log["events"][0].update(digest="sha256:" + "0" * 64))
        self.assertDiagnostics(["registry/lifecycle.json: event-digest-mismatch: event 1: asn1.der.000009"])

    def test_tampered_definition(self):
        self.edit(case_path("asn1.der.000007"), lambda case: case["normative"]["input"].update(octets="0101ff"))
        self.assertDiagnostics(["registry/lifecycle.json: event-digest-mismatch: event 2: asn1.der.000007"])

    def test_forbidden_transition_and_unknown_case(self):
        def change(log):
            log["events"].append(dict(log["events"][1], seq=3, event="reinstate",
                                      reason={"code": "deprecation-withdrawn", "detail": "x"}))
            del log["events"][2]["replacement"]
            log["events"].append(dict(log["events"][2], seq=4, case="asn1.der.000010"))

        self.edit("registry/lifecycle.json", change)
        self.assertDiagnostics([
            "registry/lifecycle.json: event-case-unknown: event 4: asn1.der.000010",
            "registry/lifecycle.json: transition-forbidden: event 3: asn1.der.000007: reinstate of a invalid case",
        ])

    def test_schema_errors(self):
        self.edit("registry/lifecycle.json", lambda log: log.update(phase="1.x"))
        self.assertDiagnostics(["registry/lifecycle.json: schema: /phase (enum)"])


class RequiredFileTest(CorpusTestCase):
    def test_license_registry_and_log(self):
        self.path("LICENSE").unlink()
        self.path("registry/lifecycle.json").unlink()
        self.assertDiagnostics(["LICENSE: file-missing", "registry/lifecycle.json: file-missing"])
        self.path("registry/registry.json").unlink()
        self.assertDiagnostics(["LICENSE: file-missing", "registry/registry.json: file-missing"])

    def test_schema_files(self):
        self.edit("schemas/lifecycle-1.schema.json",
                  lambda schema: schema.update({"$id": "urn:opentlv:corpus:schema:lifecycle:2"}))
        self.assertDiagnostics(["schemas/lifecycle-1.schema.json: schema-id-mismatch: $id is not "
                                "urn:opentlv:corpus:schema:lifecycle:1"])
        self.path("schemas/lifecycle-1.schema.json").unlink()
        self.assertDiagnostics(["schemas/lifecycle-1.schema.json: file-missing"])

    def test_invalid_schema(self):
        self.edit("schemas/lifecycle-1.schema.json", lambda schema: schema.update(type="record"))
        diagnostics = lines(self.root)
        self.assertEqual(len(diagnostics), 1)
        self.assertTrue(diagnostics[0].startswith("schemas/lifecycle-1.schema.json: schema-invalid: "))

    def test_registry_rules(self):
        self.edit("registry/registry.json", lambda registry: registry["licenses"].append(registry["licenses"][0]))
        self.assertDiagnostics(["registry/registry.json: registry-duplicate: licenses: MIT"])

    def test_example_set_is_checked_when_present(self):
        shutil.copytree(validate.ROOT / "examples" / "valid", self.path("examples/valid"))
        self.assertDiagnostics(["examples/lifecycle.json: file-missing"])
        shutil.copyfile(validate.ROOT / "examples" / "lifecycle.json", self.path("examples/lifecycle.json"))
        self.assertDiagnostics([])
        self.assertEqual(set(validate.validate(self.root).sets), {"conformance", "examples"})


if __name__ == "__main__":
    unittest.main()
