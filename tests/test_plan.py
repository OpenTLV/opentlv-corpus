# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import contextlib
import copy
import dataclasses
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

from corpus_fixture import REVISION, case_path, read_json, write_corpus, write_json
from scripts import canonical, check_contract, plan, release, validate

ROOT = check_contract.ROOT
STUB = [sys.executable, "-B", str(ROOT / "tests" / "stub_adapter.py")]
EXAMPLES = {name: canonical.load_json(ROOT / "examples" / "plans" / name)
            for name in ("full.json", "smoke.json", "filtered.json", "explicit.json")}
STANDARD = "itu-t-x690-2021-02"
# The descriptor of tests/stub_adapter.py: strict DER decoding and encoding of trees.
DESCRIPTOR = {
    "protocol": 1,
    "adapter": {"name": "opentlv-corpus-stub-adapter", "version": "1.0.0"},
    "implementation": {"name": "stub-der-tree", "version": "1.0.0"},
    "supports": [{"standard": STANDARD, "rules": ["der"], "operations": ["decode", "encode", "round-trip"],
                  "strictness": ["strict"], "views": ["tree"]}],
    "provides": [],
    "limits": {"max_message_octets": 1048576, "max_input_octets": 65536, "max_depth": 32}}
# Cases tagged smoke in the fixture.
SMOKE = ("asn1.der.000001", "asn1.der.000006", "asn1.ber.000001")


def document(select, **members):
    return {"schema": plan.PLAN, "name": "test", **members, "select": select}


def load(select, **members):
    return plan.load_plan(document(select, **members))


def write_fixture(directory):
    """Write the fixture corpus with the smoke tag on the SMOKE cases and return its root."""
    root = write_corpus(directory)
    for case_id in SMOKE:
        case = read_json(root / case_path(case_id))
        case["informative"]["tags"].append("smoke")
        write_json(root / case_path(case_id), case)
    return root


def build(root, output, version="0.1.0"):
    paths = release.build(root, version, output, revision=REVISION)
    archive = next(path for path in paths if path.suffix == ".tar")
    opened, problems = release.open_release(archive, Path(output) / release.SUMS)
    assert not problems, problems
    return opened, archive


def active_checks(opened, statuses=("active",)):
    """Return the IDs of every check of the cases with one of the statuses, in resolution order."""
    conformance = opened.corpus.sets["conformance"]
    status = {entry["id"]: entry["status"] for entry in conformance.projection["cases"]}
    cases = sorted(conformance.cases.values(), key=lambda case: case["id"])
    return [f"{case['id']}/{assertion['key']}" for case in cases if status[case["id"]] in statuses
            for assertion in case["normative"]["assertions"]]


class FixtureTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.directory = Path(cls._directory.name)
        cls.root = write_fixture(cls.directory / "corpus")
        cls.release, cls.archive = build(cls.root, cls.directory / "release")

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def resolve(self, select, descriptor=DESCRIPTOR, **members):
        resolved, problems = plan.resolve(load(select, **members), self.release, descriptor)
        self.assertEqual(problems, [])
        return resolved

    def problems(self, select, descriptor=DESCRIPTOR, **members):
        resolved, problems = plan.resolve(load(select, **members), self.release, descriptor)
        self.assertIsNone(resolved)
        return problems


class SchemaTest(unittest.TestCase):
    def test_selection_copies_are_identical(self):
        definitions = {kind: canonical.load_json(ROOT / "schemas" / f"{kind}-1.schema.json")["$defs"]
                       for kind in ("plan", "registry")}
        for name in ("key", "keys", "selection"):
            self.assertEqual(definitions["plan"][name], definitions["registry"][name], name)

    def test_shared_definitions_match_their_sources(self):
        resolution = canonical.load_json(ROOT / "schemas" / "resolution-1.schema.json")["$defs"]
        manifest = canonical.load_json(ROOT / "schemas" / "manifest-1.schema.json")["$defs"]
        capabilities = canonical.load_json(ROOT / "protocol" / "capabilities-1.schema.json")["$defs"]
        plan_schema = canonical.load_json(ROOT / "schemas" / "plan-1.schema.json")
        self.assertEqual(resolution["version"], manifest["version"])
        self.assertEqual(plan_schema["$defs"]["version"], manifest["version"])
        self.assertEqual(resolution["component"], capabilities["component"])
        self.assertEqual(resolution["milliseconds"], plan_schema["$defs"]["milliseconds"])
        self.assertEqual(canonical.load_json(ROOT / "schemas" / "resolution-1.schema.json")["properties"]["limits"]
                         ["properties"], plan_schema["properties"]["limits"]["properties"])

    def test_problem_codes_are_the_protocol_codes_decided_before_sending(self):
        check = canonical.load_json(ROOT / "schemas" / "resolution-1.schema.json")["$defs"]["check"]
        self.assertEqual(check["properties"]["problem"]["enum"], ["undeclared", "capability", "input-too-large",
                                                                   "too-deep"])


class LoadTest(unittest.TestCase):
    def test_examples_round_trip(self):
        for name, example in EXAMPLES.items():
            with self.subTest(name):
                loaded = plan.load_plan(example)
                self.assertEqual(loaded.to_json(), example)
                self.assertEqual(plan.load_plan(loaded.to_json()), loaded)
                self.assertEqual(loaded.digest, plan.json_digest(example))

    def test_plans_are_immutable(self):
        loaded = plan.load_plan(EXAMPLES["filtered.json"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            loaded.name = "other"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            loaded.select.rules = ("ber",)
        self.assertIsInstance(loaded.select.lifecycle, tuple)
        self.assertEqual(hash(loaded), hash(plan.load_plan(EXAMPLES["filtered.json"])))

    def test_digest_ignores_formatting_and_member_order(self):
        example = EXAMPLES["filtered.json"]
        reordered = json.loads(json.dumps(dict(reversed(list(example.items())))))
        self.assertEqual(plan.load_plan(reordered).digest, plan.load_plan(example).digest)
        self.assertNotEqual(load({"rules": ["ber"]}).digest, load({"rules": ["der"]}).digest)

    def test_selection_cannot_depend_on_results_or_comparison(self):
        # Selection reads definitions, tags and lifecycle only: no result, verdict or comparison mode.
        self.assertEqual([field.name for field in dataclasses.fields(plan.Selection)],
                         ["suite", "cases", "standards", "rules", "operations", "strictness", "views", "tags",
                          "exclude_tags", "lifecycle"])
        for member in ({"verdict": ["fail"]}, {"status": ["pass"]}, {"previous": "resolved.json"}):
            with self.subTest(member), self.assertRaises(plan.PlanError) as raised:
                load(member)
            self.assertEqual(raised.exception.problems[0][0], "schema")
        with self.assertRaises(plan.PlanError) as raised:
            plan.load_plan(document({}, comparison={"mode": "independent"}))
        self.assertEqual(raised.exception.problems, [("schema", "/ (additionalProperties)")])

    def test_invalid_cases_are_never_selectable(self):
        with self.assertRaises(plan.PlanError) as raised:
            load({"lifecycle": ["active", "invalid"]})
        self.assertEqual(raised.exception.problems, [("schema", "/select/lifecycle/1 (enum)")])

    def test_rejected_documents(self):
        for name, doc, code in (
                ("schema version", dict(document({}), schema="urn:opentlv:corpus:schema:plan:2"), "schema-unsupported"),
                ("not an object", [], "schema-unsupported"),
                ("float limit", document({}, limits={"execute_timeout_ms": 5.0}), "not-canonicalizable"),
                ("empty list", document({"tags": []}), "schema"),
                ("malformed check ID", document({"cases": ["asn1.der.1"]}), "schema"),
                ("pin without digest", document({}, corpus={"release": "0.1.0"}), "schema")):
            with self.subTest(name), self.assertRaises(plan.PlanError) as raised:
                plan.load_plan(doc)
            self.assertEqual({problem[0] for problem in raised.exception.problems}, {code})

    def test_limits(self):
        self.assertEqual(plan.Limits().effective(), plan.Limits(10000, 10000, 5000, 4 * 1024 * 1024, 64 * 1024))
        limits = plan.Limits(execute_timeout_ms=2500, stderr_octets=0).effective()
        self.assertEqual((limits.execute_timeout_ms, limits.hello_timeout_ms, limits.stderr_octets), (2500, 10000, 0))
        harness = limits.harness()
        self.assertEqual((harness.execute_timeout, harness.hello_timeout, harness.stderr_octets), (2.5, 10.0, 0))


class ResolveTest(FixtureTestCase):
    def test_full_plan_keeps_every_active_check(self):
        resolved = self.resolve(EXAMPLES["full.json"]["select"])
        self.assertEqual([check.id for check in resolved.checks], active_checks(self.release))
        ids = {check.case for check in resolved.checks}
        self.assertNotIn("asn1.der.000007", ids)  # invalid
        self.assertNotIn("asn1.der.000009", ids)  # deprecated
        decisions = {check.id: (check.applicability, check.problem) for check in resolved.checks}
        self.assertEqual(decisions["asn1.der.000001/decode"], ("applicable", None))
        self.assertEqual(decisions["asn1.der.000004/lenient"], ("unsupported", "undeclared"))
        self.assertEqual(decisions["asn1.der.000002/decode"], ("unsupported", "undeclared"))
        self.assertEqual(decisions["asn1.ber.000001/decode"], ("unsupported", "undeclared"))
        for check in resolved.checks:
            self.assertEqual(check.digest, self.release.corpus.sets["conformance"].digests[case_path(check.case)])

    def test_capability_and_limit_decisions(self):
        descriptor = copy.deepcopy(DESCRIPTOR)
        descriptor["supports"][0]["rules"] = ["ber", "der"]
        resolved = self.resolve({"cases": ["asn1.ber.000001"]}, descriptor)
        self.assertEqual([(check.applicability, check.problem, check.detail) for check in resolved.checks],
                         [("unsupported", "capability", "tag-number.over-31-bits")])
        descriptor["provides"] = ["tag-number.over-31-bits"]
        descriptor["limits"]["max_input_octets"] = 2
        resolved = self.resolve({"cases": ["asn1.ber.000001"]}, descriptor)
        self.assertEqual([(check.applicability, check.problem) for check in resolved.checks],
                         [("unsupported", "input-too-large")])

    def test_resolution_is_deterministic(self):
        first = plan.resolve(plan.load_plan(EXAMPLES["full.json"]), self.release, DESCRIPTOR)[0]
        second = plan.resolve(plan.load_plan(EXAMPLES["full.json"]), self.release, json.loads(
            json.dumps(dict(reversed(list(DESCRIPTOR.items()))))))[0]
        self.assertEqual(first.dumps(), second.dumps())
        rebuilt, _ = build(self.root, self.directory / "rebuilt")
        third = plan.resolve(plan.load_plan(EXAMPLES["full.json"]), rebuilt, DESCRIPTOR)[0]
        self.assertEqual(first.dumps(), third.dumps())
        self.assertEqual(first.identity, third.identity)

    def test_resolved_plan_records_its_inputs(self):
        example = plan.load_plan(EXAMPLES["filtered.json"])
        resolved = plan.resolve(example, self.release, DESCRIPTOR)[0]
        self.assertEqual(resolved.plan, plan.PlanRef("der-strict-decode", example.digest))
        self.assertEqual(resolved.corpus, plan.CorpusRef("0.1.0", validate.sha256(self.archive.read_bytes()),
                                                         REVISION))
        self.assertEqual(resolved.tools, plan.Component(**release.TOOLS))
        self.assertEqual((resolved.adapter.protocol, str(resolved.adapter), resolved.adapter.descriptor),
                         (1, "opentlv-corpus-stub-adapter 1.0.0 for stub-der-tree 1.0.0",
                          plan.json_digest(DESCRIPTOR)))
        self.assertEqual(resolved.limits, plan.Limits(10000, 30000, 10000, 4 * 1024 * 1024, 64 * 1024))
        # No resolution time or other run-dependent member.
        self.assertEqual(list(resolved.to_json()), ["schema", "plan", "corpus", "tools", "adapter", "limits", "checks"])

    def test_smoke_suite(self):
        resolved = self.resolve(EXAMPLES["smoke.json"]["select"])
        self.assertEqual(sorted({check.case for check in resolved.checks}), sorted(SMOKE))
        self.assertEqual([check.id for check in resolved.checks],
                         [check for check in active_checks(self.release) if check.split("/")[0] in SMOKE])
        narrowed = self.resolve({"suite": "smoke", "rules": ["der"], "operations": ["decode"]})
        self.assertEqual([check.id for check in narrowed.checks], ["asn1.der.000001/decode"])

    def test_filters(self):
        resolved = self.resolve(EXAMPLES["filtered.json"]["select"])
        self.assertEqual([check.id for check in resolved.checks],
                         ["asn1.der.000001/decode", "asn1.der.000009/decode"])
        self.assertEqual([check.status for check in resolved.checks], ["active", "deprecated"])
        tagged = self.resolve({"tags": ["typed-value", "integer"], "views": ["value"]})
        self.assertEqual([check.id for check in tagged.checks],
                         ["asn1.ber.000002/decode", "asn1.ber.000002/round-trip-value", "asn1.der.000002/decode",
                          "asn1.der.000003/encode", "asn1.der.000003/round-trip"])

    def test_explicit_ids(self):
        resolved = self.resolve(EXAMPLES["explicit.json"]["select"])
        self.assertEqual([check.id for check in resolved.checks],
                         ["asn1.der.000001/decode", "asn1.der.000001/round-trip", "asn1.der.000004/strict"])
        self.assertEqual(self.problems({"cases": ["asn1.der.000007", "asn1.der.000009/decode"]}), [
            ("explicit-excluded", "asn1.der.000007 (invalid) is named but the other conditions exclude it"),
            ("explicit-excluded", "asn1.der.000009/decode (deprecated) is named but the other conditions exclude it"),
        ])
        self.assertEqual(self.problems({"cases": ["asn1.der.000001"], "operations": ["encode"]})[0][0],
                         "explicit-excluded")
        self.assertEqual(self.problems({"cases": ["asn1.der.000001"], "suite": "smoke", "rules": ["ber"]})[0][0],
                         "explicit-excluded")
        self.assertEqual([check.id for check in self.resolve({"cases": ["asn1.der.000009"],
                                                              "lifecycle": ["deprecated"]}).checks],
                         ["asn1.der.000009/decode"])

    def test_unknown_names_fail_before_running(self):
        self.assertEqual(self.problems({"cases": ["asn1.der.000099", "asn1.der.000001/encode"]}),
                         [("case-unknown", "asn1.der.000099"), ("assertion-unknown", "asn1.der.000001/encode")])
        self.assertEqual(self.problems({"suite": "nightly"}), [("suite-unknown", "nightly")])
        self.assertEqual(self.problems({"standards": ["itu-t-x690-2015-08"], "rules": ["per"]}),
                         [("standard-unknown", "itu-t-x690-2015-08"), ("rules-unknown", "per")])

    def test_empty_selection_is_an_error(self):
        self.assertEqual(self.problems({"tags": ["no-such-tag"]}), [("selection-empty", "plan test selects no check")])

    def test_corpus_pin(self):
        digest = validate.sha256(self.archive.read_bytes())
        self.resolve({}, corpus={"release": "0.1.0", "sha256": digest})
        for pin in ({"release": "0.1.1", "sha256": digest}, {"release": "0.1.0", "sha256": "0" * 64}):
            with self.subTest(pin):
                self.assertEqual([code for code, _ in self.problems({}, corpus=pin)], ["corpus-mismatch"])

    def test_descriptor_versions(self):
        self.assertEqual([code for code, _ in self.problems({}, dict(DESCRIPTOR, protocol=2))],
                         ["protocol-unsupported"])
        self.assertEqual([code for code, _ in self.problems({}, dict(DESCRIPTOR, supports=[]))],
                         ["descriptor-invalid"])


class ResolvedTest(FixtureTestCase):
    def setUp(self):
        self.resolved = plan.resolve(plan.load_plan(EXAMPLES["full.json"]), self.release, DESCRIPTOR)[0]

    def test_serialization_preserves_identity(self):
        loaded = plan.load_resolved(json.loads(self.resolved.dumps()))
        self.assertEqual(loaded, self.resolved)
        self.assertEqual(loaded.identity, self.resolved.identity)
        self.assertEqual(loaded.dumps(), self.resolved.dumps())
        compact = json.dumps(dict(reversed(list(self.resolved.to_json().items()))), separators=(",", ":"))
        self.assertEqual(plan.load_resolved(json.loads(compact)).identity, self.resolved.identity)
        self.assertEqual(self.resolved.identity, plan.json_digest(json.loads(self.resolved.dumps())))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            self.resolved.checks[0].applicability = "applicable"
        self.assertIsInstance(self.resolved.checks, tuple)

    def test_resolved_schema(self):
        document = self.resolved.to_json()
        states = [check["applicability"] for check in document["checks"]]
        unsupported, applicable = states.index("unsupported"), states.index("applicable")
        for change, code in ((lambda doc: doc["checks"][unsupported].pop("problem"), "schema"),
                             (lambda doc: doc["checks"][applicable].update(problem="capability"), "schema"),
                             (lambda doc: doc["checks"][0].update(status="invalid"), "schema"),
                             (lambda doc: doc.update(checks=[]), "schema"),
                             (lambda doc: doc.update(created="2026-11-06T00:00:00Z"), "schema"),
                             (lambda doc: doc.update(schema="urn:opentlv:corpus:schema:resolution:2"),
                              "schema-unsupported")):
            changed = copy.deepcopy(document)
            change(changed)
            with self.assertRaises(plan.PlanError) as raised:
                plan.load_resolved(changed)
            self.assertEqual(raised.exception.problems[0][0], code)

    def test_check_before_running(self):
        self.assertEqual(plan.check_resolved(self.resolved, self.release, DESCRIPTOR), [])

    def test_new_cases_do_not_change_a_resolved_plan(self):
        snapshot = self.resolved.dumps()
        with tempfile.TemporaryDirectory() as directory:
            root = write_fixture(Path(directory) / "corpus")
            case = read_json(root / case_path("asn1.der.000006"))
            case["id"] = "asn1.der.000010"
            write_json(root / case_path(case["id"]), case)
            registry = read_json(root / "registry" / "registry.json")
            next(item for item in registry["namespaces"] if item["id"] == "asn1.der")["allocated"] = 10
            write_json(root / "registry" / "registry.json", registry)
            newer, _ = build(root, Path(directory) / "release", "0.2.0")
        self.assertEqual([code for code, _ in plan.check_resolved(self.resolved, newer, DESCRIPTOR)],
                         ["corpus-mismatch"])
        again = plan.resolve(plan.load_plan(EXAMPLES["full.json"]), newer, DESCRIPTOR)[0]
        self.assertEqual(len(again.checks), len(self.resolved.checks) + 1)
        self.assertIn("asn1.der.000010/encode", [check.id for check in again.checks])
        self.assertEqual(self.resolved.dumps(), snapshot)

    def test_adapter_mismatch(self):
        changed = copy.deepcopy(DESCRIPTOR)
        changed["adapter"]["version"] = "1.0.1"
        self.assertEqual([code for code, _ in plan.check_resolved(self.resolved, self.release, changed)],
                         ["adapter-mismatch"])
        changed = copy.deepcopy(DESCRIPTOR)
        changed["provides"] = ["tag-number.over-31-bits"]
        self.assertEqual([code for code, _ in plan.check_resolved(self.resolved, self.release, changed)],
                         ["adapter-mismatch"])
        self.assertEqual([code for code, _ in plan.check_resolved(self.resolved, self.release,
                                                                  dict(DESCRIPTOR, protocol=2))],
                         ["protocol-unsupported"])

    def test_tampered_checks(self):
        checks = list(self.resolved.checks)
        index = next(i for i, check in enumerate(checks) if check.applicability == "unsupported")
        flipped = dataclasses.replace(checks[index], applicability="applicable", problem=None, detail=None)
        for name, changed, code in (
                ("decision", checks[:index] + [flipped] + checks[index + 1:], "check-mismatch"),
                ("digest", [dataclasses.replace(checks[0], digest="sha256:" + "0" * 64)] + checks[1:],
                 "check-mismatch"),
                ("order", [checks[1], checks[0]] + checks[2:], "check-order"),
                ("repeat", checks + [checks[-1]], "check-order"),
                ("unknown", checks + [dataclasses.replace(checks[-1], case="asn1.der.000099")], "check-unknown")):
            with self.subTest(name):
                tampered = dataclasses.replace(self.resolved, checks=tuple(changed))
                self.assertEqual([code_ for code_, _ in plan.check_resolved(tampered, self.release, DESCRIPTOR)],
                                 [code])


class CommandTest(FixtureTestCase):
    def run_main(self, *args):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            status = plan.main([str(arg) for arg in args])
        return status, errors.getvalue()

    def test_resolve_and_check_with_the_adapter(self):
        output = self.directory / "resolved.json"
        sums = self.archive.parent / release.SUMS
        status, errors = self.run_main("resolve", "--corpus", self.archive, "--sums", sums,
                                       "--plan", ROOT / "examples" / "plans" / "smoke.json", "--output", output,
                                       "--", *STUB)
        self.assertEqual(status, 0, errors)
        resolved = plan.load_resolved(canonical.load_json(output))
        self.assertEqual(output.read_bytes(), resolved.dumps())
        self.assertEqual(errors, plan.describe(resolved) + "\n")
        self.assertTrue(errors.startswith("plan smoke: 4 checks, 3 applicable, 1 unsupported; corpus 0.1.0; "
                                          "adapter opentlv-corpus-stub-adapter 1.0.0 for stub-der-tree 1.0.0; "
                                          "identity sha256:"))
        self.assertEqual(self.run_main("check", "--corpus", self.archive, "--resolved", output, "--", *STUB)[0], 0)
        descriptor = self.directory / "descriptor.json"
        write_json(descriptor, DESCRIPTOR)
        self.assertEqual(self.run_main("check", "--corpus", self.archive, "--resolved", output,
                                       "--descriptor", descriptor)[0], 0)

    def test_problems_are_reported(self):
        descriptor = self.directory / "descriptor-2.json"
        write_json(descriptor, dict(DESCRIPTOR, protocol=2))
        status, errors = self.run_main("resolve", "--corpus", self.archive, "--descriptor", descriptor,
                                       "--plan", ROOT / "examples" / "plans" / "full.json")
        self.assertEqual(status, 1)
        self.assertEqual(errors, "protocol-unsupported: protocol 2; this tooling speaks 1\n"
                                 "plan not runnable: 1 problems\n")

    def test_release_must_verify(self):
        sums = self.directory / "wrong-sums"
        sums.write_bytes(validate.format_sums({self.archive.name: "0" * 64}))
        status, errors = self.run_main("resolve", "--corpus", self.archive, "--sums", sums,
                                       "--descriptor", self.directory / "missing.json",
                                       "--plan", ROOT / "examples" / "plans" / "full.json")
        self.assertEqual(status, 1)
        self.assertTrue(errors.startswith(f"release-invalid: {self.archive.name}: checksum-mismatch: "), errors)


class SuiteTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = write_fixture(Path(directory.name) / "corpus")
        self.registry = read_json(self.root / "registry" / "registry.json")

    def diagnostics(self, suite):
        self.registry["suites"].append(suite)
        write_json(self.root / "registry" / "registry.json", self.registry)
        return [str(diagnostic) for diagnostic in validate.validate(self.root).diagnostics]

    def test_registered_smoke_suite(self):
        registry = canonical.load_json(ROOT / "registry" / "registry.json")
        self.assertEqual([(suite["id"], suite["select"]) for suite in registry["suites"]],
                         [("smoke", {"tags": ["smoke"]})])

    def test_named_cases_must_exist_and_be_selected(self):
        self.assertEqual(self.diagnostics({"id": "pinned", "title": "Pinned", "select": {"cases": [
            "asn1.der.000001/decode", "asn1.der.000099", "asn1.der.000001/encode", "asn1.der.000009"]}}), [
            "registry/registry.json: suite-case-excluded: pinned: asn1.der.000009 is deprecated or outside the "
            "suite's other conditions",
            "registry/registry.json: suite-case-unknown: pinned: asn1.der.000001/encode",
            "registry/registry.json: suite-case-unknown: pinned: asn1.der.000099",
        ])

    def test_registry_rules(self):
        self.assertEqual(self.diagnostics({"id": "smoke", "title": "Again", "select": {"rules": ["xer"]}}), [
            "registry/registry.json: registry-duplicate: suites: smoke",
            "registry/registry.json: suite-rules-unknown: smoke: xer",
        ])

    def test_suites_are_never_removed(self):
        new = copy.deepcopy(self.registry)
        new["suites"] = []
        self.assertEqual(check_contract.check_registry_transition(self.registry, new), [("suite-removed", "smoke")])


if __name__ == "__main__":
    unittest.main()
