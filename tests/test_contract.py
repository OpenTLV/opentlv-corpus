# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import copy
import json
import unittest

from jsonschema import Draft202012Validator

from scripts import check_contract as contract

REGISTRY = contract.load_json(contract.REGISTRY)
VALID, INVALID = contract.load_examples()


def namespace(registry, name):
    return next(entry for entry in registry["namespaces"] if entry["id"] == name)


def codes(problems):
    return sorted({code for code, _ in problems})


class SchemaTest(unittest.TestCase):
    def test_schemas_are_valid_draft_2020_12_and_versioned(self):
        for kind in ("case", "registry", "manifest"):
            schema = contract.load_json(contract.SCHEMAS / f"{kind}-1.schema.json")
            Draft202012Validator.check_schema(schema)
            self.assertEqual(schema["$id"], f"urn:opentlv:corpus:schema:{kind}:1")
            self.assertEqual(schema["properties"]["schema"], {"const": schema["$id"]})
            self.assertFalse(schema["additionalProperties"])

    def test_committed_registry_and_examples_satisfy_the_contract(self):
        self.assertEqual(contract.run(), [])

    def test_every_invalid_example_breaks_exactly_its_declared_rule(self):
        names = [entry["name"] for entry, _ in INVALID]
        self.assertEqual(len(names), len(set(names)))
        for entry, document in INVALID:
            with self.subTest(entry["name"]):
                stage, found = contract.violations(document, REGISTRY)
                ((expected_stage, expected),) = entry["violation"].items()
                self.assertEqual((stage, found), (expected_stage, sorted(expected)))
                base = VALID[entry["base"]]
                self.assertEqual(contract.violations(base, REGISTRY), ("semantic", []))

    def test_valid_examples_cover_the_contract(self):
        assertions = [assertion for case in VALID.values() for assertion in case["normative"]["assertions"]]
        results = [result for assertion in assertions for result in contract._results(assertion["expect"])]
        self.assertEqual({a["request"]["operation"] for a in assertions}, {"decode", "encode", "round-trip"})
        self.assertEqual({a["request"]["strictness"] for a in assertions}, {"strict", "lenient"})
        self.assertEqual({a["request"]["view"] for a in assertions}, {"tree", "value"})
        self.assertEqual({next(iter(case["normative"]["input"])) for case in VALID.values()}, {"octets", "tree", "value"})
        self.assertEqual({r["outcome"] for r in results}, {"accept", "reject"})
        self.assertEqual({a["expect"].get("basis") for a in assertions} - {None},
                         {"permitted-alternatives", "implementation-defined"})
        self.assertEqual({r["preserves"] for r in results if "preserves" in r}, {"octets", "tree", "value"})
        self.assertTrue({"tree", "value", "octets"} <= {key for r in results for key in r})
        self.assertTrue(any("requires" in a for a in assertions))

    def test_example_file_names_are_unique_per_id(self):
        ids = [case["id"] for case in VALID.values()]
        self.assertEqual(len(ids), len(set(ids)))
        for case in VALID.values():
            self.assertTrue(case["id"].startswith(contract.EXAMPLE_FAMILY + "."))

    def test_normative_content_is_safe_for_canonical_json(self):
        def numbers(node):
            if isinstance(node, bool):
                return []
            if isinstance(node, (int, float)):
                return [node]
            if isinstance(node, dict):
                return [n for value in node.values() for n in numbers(value)]
            if isinstance(node, list):
                return [n for value in node for n in numbers(value)]
            return []

        for name, case in VALID.items():
            for number in numbers(case["normative"]):
                with self.subTest(name):
                    self.assertIsInstance(number, int)
                    self.assertLess(abs(number), 2 ** 53)


class IdentityTest(unittest.TestCase):
    def test_id_grammar(self):
        for case_id, parsed in (("asn1.der.000001", ("asn1.der", 1)),
                                ("asn1.ber.999999", ("asn1.ber", 999999)),
                                ("emv1.tlv.000042", ("emv1.tlv", 42))):
            self.assertEqual(contract.parse_id(case_id), parsed)
            self.assertEqual(contract.format_id(*parsed), case_id)
        for case_id in ("asn1.der.000000", "asn1.der.1", "asn1.der.0000001", "ASN1.der.000001",
                        "asn1.der.length.000001", "asn1.000001", "1asn.der.000001", "asn1.der-x.000001",
                        "asn1.der.00000a", " asn1.der.000001", "asn1.der.000001\n", None, 1):
            with self.subTest(case_id=case_id):
                self.assertIsNone(contract.parse_id(case_id))
                if isinstance(case_id, str):
                    errors = contract.schema_errors("case", {**VALID["decode-tree.json"], "id": case_id})
                    self.assertTrue(errors)

    def test_next_id_allocates_sequentially_and_never_reuses(self):
        case_id, registry = contract.next_id(REGISTRY, "asn1.der")
        self.assertEqual(case_id, "asn1.der.000001")
        self.assertEqual(namespace(REGISTRY, "asn1.der")["allocated"], 0)
        self.assertEqual(contract.check_registry_transition(REGISTRY, registry), [])
        # Removing the case later does not lower the allocation, so the serial is not handed out again.
        case_id, registry = contract.next_id(registry, "asn1.der")
        self.assertEqual(case_id, "asn1.der.000002")

    def test_next_id_refuses_unknown_closed_and_exhausted_namespaces(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            contract.next_id(REGISTRY, "asn1.xer")
        closed = copy.deepcopy(REGISTRY)
        namespace(closed, "asn1.der")["closed"] = True
        with self.assertRaisesRegex(ValueError, "closed"):
            contract.next_id(closed, "asn1.der")
        full = copy.deepcopy(REGISTRY)
        namespace(full, "asn1.der")["allocated"] = 999999
        with self.assertRaisesRegex(ValueError, "exhausted"):
            contract.next_id(full, "asn1.der")

    def test_cases_have_unique_ids_and_known_relations(self):
        cases = list(VALID.values())
        self.assertEqual(contract.check_cases(cases, REGISTRY), [])
        duplicate = copy.deepcopy(cases[0])
        self.assertEqual(codes(contract.check_cases(cases + [duplicate], REGISTRY)), ["duplicate-id"])
        dangling = copy.deepcopy(cases[0])
        dangling["informative"]["related"] = ["example.der.000006"]
        others = [case for case in cases if case["id"] not in (cases[0]["id"], "example.der.000006")]
        self.assertEqual(codes(contract.check_cases(others + [dangling], REGISTRY)), ["related-unknown"])


class RegistryTest(unittest.TestCase):
    def mutated(self, change):
        registry = copy.deepcopy(REGISTRY)
        change(registry)
        return registry

    def test_committed_registry_is_consistent(self):
        self.assertEqual(contract.schema_errors("registry", REGISTRY), [])
        self.assertEqual(contract.check_registry(REGISTRY), [])

    def test_registry_problems(self):
        cases = [
            ("registry-duplicate", lambda r: r["namespaces"].append(copy.deepcopy(r["namespaces"][0]))),
            ("registry-duplicate", lambda r: r["licenses"].append(copy.deepcopy(r["licenses"][0]))),
            ("namespace-purpose", lambda r: namespace(r, "asn1.der").update(purpose="example")),
            ("namespace-standard-unknown",
             lambda r: namespace(r, "asn1.der")["standards"].append("itu-t-x690-2015-08")),
            ("namespace-rules-unsupported", lambda r: namespace(r, "asn1.der").update(rules="xer")),
            ("reason-parent-missing", lambda r: r["reasons"].append({"code": "framing.missing", "description": "x"})),
        ]
        for index, (code, change) in enumerate(cases):
            with self.subTest(index=index, code=code):
                registry = self.mutated(change)
                self.assertEqual(contract.schema_errors("registry", registry), [])
                self.assertEqual(codes(contract.check_registry(registry)), [code])

    def test_conformance_namespaces_cannot_use_the_example_family(self):
        registry = self.mutated(lambda r: namespace(r, "example.der").update(purpose="conformance"))
        self.assertEqual(codes(contract.check_registry(registry)), ["namespace-purpose"])

    def test_registry_transitions_preserve_identity(self):
        def add_namespace(r):
            r["namespaces"].append({"id": "asn1.xer", "purpose": "conformance", "title": "x", "rules": "der",
                                    "standards": ["itu-t-x690-2021-02"], "allocated": 0})

        allowed = {
            "allocate": lambda r: namespace(r, "asn1.ber").update(allocated=5),
            "add namespace": add_namespace,
            "add reason": lambda r: r["reasons"].append({"code": "input.extra", "description": "x"}),
            "deprecate reason": lambda r: r["reasons"][1].update(deprecated=True),
            "retitle namespace": lambda r: namespace(r, "asn1.ber").update(title="Basic Encoding Rules"),
            "close namespace": lambda r: namespace(r, "asn1.ber").update(closed=True),
            "add license": lambda r: r["licenses"].append({"id": "CC0-1.0", "name": "x", "url": "https://x"}),
        }
        for label, change in allowed.items():
            with self.subTest(label):
                self.assertEqual(contract.check_registry_transition(REGISTRY, self.mutated(change)), [])

        forbidden = {
            "allocation-decreased": lambda r: namespace(r, "example.der").update(allocated=5),
            "namespace-removed": lambda r: r["namespaces"].remove(namespace(r, "asn1.cer")),
            "namespace-changed": lambda r: namespace(r, "asn1.cer").update(rules="der"),
            "namespace-standard-removed": lambda r: namespace(r, "asn1.cer").update(standards=["x"]),
            "standard-removed": lambda r: r["standards"].clear(),
            "standard-changed": lambda r: r["standards"][0].update(edition="08/2015"),
            "standard-rules-removed": lambda r: r["standards"][0].update(rules=["ber", "der"]),
            "reason-removed": lambda r: r["reasons"].pop(),
            "capability-removed": lambda r: r["capabilities"].pop(),
            "license-removed": lambda r: r["licenses"].pop(),
        }
        for code, change in forbidden.items():
            with self.subTest(code):
                self.assertIn(code, codes(contract.check_registry_transition(REGISTRY, self.mutated(change))))

    def test_a_section_added_to_the_schema_is_empty_in_older_registries(self):
        older = self.mutated(lambda r: r.pop("licenses"))
        self.assertEqual(contract.check_registry_transition(older, REGISTRY), [])
        self.assertEqual(codes(contract.check_registry_transition(REGISTRY, older)), ["license-removed"])

    def test_closed_namespaces_stay_closed(self):
        closed = self.mutated(lambda r: namespace(r, "asn1.ber").update(closed=True))
        reopened = copy.deepcopy(closed)
        del namespace(reopened, "asn1.ber")["closed"]
        self.assertEqual(codes(contract.check_registry_transition(closed, reopened)), ["namespace-reopened"])
        grown = copy.deepcopy(closed)
        namespace(grown, "asn1.ber")["allocated"] = 1
        self.assertEqual(codes(contract.check_registry_transition(closed, grown)), ["namespace-closed"])


class LicenseTest(unittest.TestCase):
    def test_license_expressions(self):
        for expression, ids in (("MIT", ["MIT"]), ("MIT OR Apache-2.0", ["MIT", "Apache-2.0"]),
                                ("(MIT AND CC0-1.0) OR BSD-2-Clause", ["MIT", "CC0-1.0", "BSD-2-Clause"]),
                                ("GPL-2.0+", ["GPL-2.0+"]), ("LicenseRef-x", ["LicenseRef-x"])):
            with self.subTest(expression):
                self.assertEqual(contract.license_ids(expression), ids)
        for expression in ("", " MIT", "MIT ", "MIT AND", "AND MIT", "MIT MIT", "(MIT", "MIT)", "()", "mit or x",
                           "MIT WITH Classpath-exception-2.0", "MIT/X11", "MIT,X11", "OR"):
            with self.subTest(expression):
                self.assertIsNone(contract.license_ids(expression))

    def test_case_licenses_are_registered(self):
        case = copy.deepcopy(VALID["decode-tree.json"])
        case["informative"]["provenance"]["license"] = "MIT OR CC0-1.0"
        self.assertEqual(contract.check_case(case, REGISTRY), [("license-unregistered", "provenance: CC0-1.0")])
        case = copy.deepcopy(VALID["decode-tree.json"])
        case["informative"]["oracle"].update(source="published-vector", vectors=[
            {"publisher": "x", "name": "x", "url": "https://x", "license": "MIT"},
            {"publisher": "x", "name": "x", "url": "https://x", "license": "Proprietary"}])
        self.assertEqual(contract.schema_errors("case", case), [])
        self.assertEqual(contract.check_case(case, REGISTRY), [("license-unregistered", "vectors/1: Proprietary")])


class OracleBlindTest(unittest.TestCase):
    def test_adapter_view_never_contains_expectations_or_metadata(self):
        for name, case in VALID.items():
            for assertion in case["normative"]["assertions"]:
                with self.subTest(f"{name}/{assertion['key']}"):
                    view = contract.oracle_blind_view(case, assertion["key"])
                    self.assertLessEqual(set(view), {"check", "standard", "request", "input", "type"})
                    self.assertEqual(contract.expectation_keys(view), set())
                    self.assertEqual(view["check"], f"{case['id']}/{assertion['key']}")
                    self.assertEqual(view["request"], assertion["request"])
                    self.assertNotIn(json.dumps(assertion["expect"], sort_keys=True), json.dumps(view, sort_keys=True))

    def test_adapter_view_is_an_allow_list(self):
        case = copy.deepcopy(VALID["decode-tree.json"])
        case["normative"]["digest"] = "secret"
        case["normative"]["assertions"][0]["note"] = "secret"
        view = contract.oracle_blind_view(case, "decode")
        self.assertNotIn("secret", json.dumps(view))

    def test_view_does_not_share_state_with_the_case(self):
        case = copy.deepcopy(VALID["decode-value.json"])
        view = contract.oracle_blind_view(case, "decode")
        view["type"]["kind"] = "integer"
        view["input"]["octets"] = ""
        self.assertEqual(case, VALID["decode-value.json"])

    def test_expectation_keys_are_found_at_any_depth(self):
        self.assertEqual(contract.expectation_keys({"a": [{"b": {"expected": 1}}]}), {"expected"})


class ValueTest(unittest.TestCase):
    TYPE = {"kind": "sequence", "fields": [
        {"name": "flag", "type": {"kind": "boolean"}, "default": {"kind": "boolean", "value": False}},
        {"name": "count", "type": {"kind": "integer"}, "optional": True},
        {"name": "items", "type": {"kind": "set_of", "item": {"kind": "octet_string"}}},
        {"name": "pick", "type": {"kind": "choice", "alternatives": [
            {"name": "number", "type": {"kind": "integer"}},
            {"name": "text", "type": {"kind": "utf8_string"}}]}}]}

    def value(self, *fields):
        return {"kind": "sequence", "fields": [{"name": name, "value": value} for name, value in fields]}

    def test_conforming_values(self):
        items = ("items", {"kind": "set_of", "items": [{"kind": "octet_string", "hex": "aa"}]})
        pick = ("pick", {"kind": "choice", "alternative": "text", "value": {"kind": "utf8_string", "value": "x"}})
        self.assertEqual(contract.check_value(self.value(items, pick), self.TYPE), [])
        flag = ("flag", {"kind": "boolean", "value": True})
        count = ("count", {"kind": "integer", "value": "-1"})
        self.assertEqual(contract.check_value(self.value(flag, count, items, pick), self.TYPE), [])

    def test_fields_equal_to_their_default_are_omitted(self):
        items = ("items", {"kind": "set_of", "items": []})
        pick = ("pick", {"kind": "choice", "alternative": "number", "value": {"kind": "integer", "value": "0"}})
        flag = ("flag", {"kind": "boolean", "value": False})
        self.assertEqual(codes(contract.check_value(self.value(flag, items, pick), self.TYPE)), ["value-not-canonical"])

    def test_nonconforming_values(self):
        items = ("items", {"kind": "set_of", "items": [{"kind": "octet_string", "hex": "aa"}]})
        pick = ("pick", {"kind": "choice", "alternative": "text", "value": {"kind": "utf8_string", "value": "x"}})
        flag = ("flag", {"kind": "boolean", "value": True})
        bad = {
            "missing mandatory": self.value(items),
            "unknown field": self.value(items, pick, ("extra", {"kind": "null"})),
            "out of order": self.value(pick, items),
            "wrong item": self.value(("items", {"kind": "set_of", "items": [{"kind": "null"}]}), pick),
            "unknown alternative": self.value(items, ("pick", {"kind": "choice", "alternative": "x",
                                                               "value": {"kind": "null"}})),
            "wrong alternative type": self.value(items, ("pick", {"kind": "choice", "alternative": "number",
                                                                  "value": {"kind": "boolean", "value": True}})),
            "wrong kind": {"kind": "set", "fields": [flag[1]]},
        }
        for label, value in bad.items():
            with self.subTest(label):
                self.assertEqual(codes(contract.check_value(value, self.TYPE)), ["value-type-mismatch"])

    def test_type_defaults_and_names_are_checked(self):
        case = copy.deepcopy(VALID["decode-value.json"])
        case["normative"]["type"]["fields"][0]["default"] = {"kind": "integer", "value": "1"}
        self.assertEqual(codes(contract.check_case(case, REGISTRY)), ["value-type-mismatch"])
        case = copy.deepcopy(VALID["decode-value.json"])
        case["normative"]["type"]["fields"][1]["name"] = "flag"
        self.assertIn("type-duplicate-name", codes(contract.check_case(case, REGISTRY)))

    def test_type_must_be_used(self):
        case = copy.deepcopy(VALID["decode-tree.json"])
        case["normative"]["type"] = {"kind": "boolean"}
        self.assertEqual(codes(contract.check_case(case, REGISTRY)), ["type-unused"])

    def test_schema_rejects_inconsistent_types(self):
        base = VALID["decode-value.json"]
        types = {
            "choice with implicit tag": {"kind": "choice", "tag": {"class": "context", "number": "0", "mode": "implicit"},
                                         "alternatives": [{"name": "a", "type": {"kind": "null"}}]},
            "sequence without fields": {"kind": "sequence"},
            "primitive with fields": {"kind": "integer", "fields": [{"name": "a", "type": {"kind": "null"}}]},
            "optional with default": {"kind": "sequence", "fields": [{"name": "a", "type": {"kind": "null"},
                                                                      "optional": True, "default": {"kind": "null"}}]},
        }
        for label, type_ in types.items():
            with self.subTest(label):
                case = copy.deepcopy(base)
                case["normative"]["type"] = type_
                self.assertTrue(contract.schema_errors("case", case))

    def test_patterns_reject_a_trailing_newline(self):
        case = copy.deepcopy(VALID["decode-tree.json"])
        case["normative"]["input"]["octets"] = "0101ff\n"
        self.assertTrue(contract.schema_errors("case", case))
        case = copy.deepcopy(VALID["decode-tree.json"])
        case["id"] += "\n"
        self.assertEqual(codes(contract.check_case(case, REGISTRY)), ["id-invalid"])

    def test_bit_string_without_octets_has_no_unused_bits(self):
        case = copy.deepcopy(VALID["encode-value.json"])
        case["normative"]["type"] = {"kind": "bit_string"}
        case["normative"]["input"]["value"] = {"kind": "bit_string", "hex": "", "unused_bits": 3}
        self.assertTrue(contract.schema_errors("case", case))
        case["normative"]["input"]["value"]["unused_bits"] = 0
        self.assertEqual(contract.schema_errors("case", case), [])


class MutationTest(unittest.TestCase):
    def test_json_pointer_operations(self):
        document = {"a": [1, 2], "b/c": {"~d": 0}}
        self.assertEqual(contract.apply_mutation(document, {"op": "replace", "path": "/a/1", "value": 3})["a"], [1, 3])
        self.assertEqual(contract.apply_mutation(document, {"op": "add", "path": "/a/-", "value": 3})["a"], [1, 2, 3])
        self.assertEqual(contract.apply_mutation(document, {"op": "add", "path": "/a/0", "value": 0})["a"], [0, 1, 2])
        self.assertEqual(contract.apply_mutation(document, {"op": "remove", "path": "/b~1c/~0d"})["b/c"], {})
        self.assertEqual(document, {"a": [1, 2], "b/c": {"~d": 0}})
        with self.assertRaises(KeyError):
            contract.apply_mutation(document, {"op": "replace", "path": "/missing", "value": 1})


if __name__ == "__main__":
    unittest.main()
