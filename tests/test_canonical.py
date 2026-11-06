# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import copy
import json
import unittest

from scripts import canonical, check_contract, check_lifecycle

VALID, _ = check_contract.load_examples()
VECTORS = canonical.load_json(check_lifecycle.DIGEST_VECTORS)


class CanonicalizationTest(unittest.TestCase):
    def test_rfc8785_member_order_uses_utf16_code_units(self):
        # RFC 8785, 3.2.3: the emoji (D83D DE00 in UTF-16) sorts before U+FB33,
        # although its code point U+1F600 is larger.
        value = {"\u20ac": "Euro Sign", "\r": "Carriage Return", "\ufb33": "Hebrew Letter Dalet With Dagesh",
                 "1": "One", "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control",
                 "\u00f6": "Latin Small Letter O With Diaeresis"}
        values = json.loads(canonical.canonicalize(value), object_pairs_hook=lambda pairs: [v for _, v in pairs])
        self.assertEqual(values,
                         ["Carriage Return", "One", "Control", "Latin Small Letter O With Diaeresis", "Euro Sign",
                          "Emoji: Grinning Face", "Hebrew Letter Dalet With Dagesh"])

    def test_rfc8785_string_escapes(self):
        # RFC 8785, 3.2.2.2.
        self.assertEqual(canonical.canonicalize("\u20ac$\u000f\nA'\u0042\u0022\u005c\\\"/"),
                         '"\u20ac$\\u000f\\nA\'B\\"\\\\\\\\\\"/"')
        self.assertEqual(canonical.canonicalize("\u001f\u007f"), '"\\u001f\u007f"')

    def test_literals_integers_and_whitespace(self):
        self.assertEqual(canonical.canonicalize({"b": [True, False, None], "a": {"d": 1, "c": -7}}),
                         '{"a":{"c":-7,"d":1},"b":[true,false,null]}')
        self.assertEqual(canonical.canonicalize([canonical.MAX_INTEGER, -canonical.MAX_INTEGER]),
                         "[9007199254740991,-9007199254740991]")
        self.assertEqual(canonical.canonical_bytes({"\u00e9": ""}), '{"\u00e9":""}'.encode("utf-8"))

    def test_values_outside_the_subset_are_rejected(self):
        for value in (2 ** 53, -(2 ** 53), 1.0, float("nan"), "\ud800", {"\udfff": 1}, {1: 1}, b"00", {1, 2}):
            with self.subTest(value=value):
                with self.assertRaises(canonical.CanonicalError):
                    canonical.canonicalize(value)

    def test_strict_parser_rejects_ambiguous_json(self):
        for text in ('{"a": 1, "a": 2}', '{"a": {"b": 1, "b": 1}}', "NaN", '{"a": Infinity}'):
            with self.subTest(text=text):
                with self.assertRaises(canonical.CanonicalError):
                    canonical.loads(text)
        self.assertEqual(canonical.loads('{"a": [1, {"b": null}]}'), {"a": [1, {"b": None}]})

    def test_committed_vectors_hold(self):
        self.assertEqual(check_lifecycle.check_digest_vectors(VECTORS, VALID), [])
        names = {vector["name"] for vector in VECTORS["canonicalization"]}
        self.assertTrue({"rfc8785-member-order", "rfc8785-string-escapes"} <= names)
        self.assertTrue(any(vector.get("rejected") for vector in VECTORS["canonicalization"]))
        self.assertEqual(set(VECTORS["cases"]), set(VALID))
        self.assertEqual({edit["effect"] for edit in VECTORS["edits"]}, {"changed", "unchanged"})

    def test_tampered_vectors_fail(self):
        vectors = copy.deepcopy(VECTORS)
        vectors["cases"]["encode-tree.json"] = "sha256:" + "0" * 64
        vectors["canonicalization"][0]["sha256"] = "0" * 64
        vectors["edits"][0]["effect"] = "changed"
        self.assertEqual(len(check_lifecycle.check_digest_vectors(vectors, VALID)), 3)


class DigestTest(unittest.TestCase):
    def test_digest_format_and_scope(self):
        case = VALID["decode-tree.json"]
        digest = canonical.digest(case)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(canonical.definition(case), {"schema": case["schema"], "id": case["id"],
                                                      "normative": case["normative"]})

    def test_informative_metadata_and_unknown_members_never_change_the_digest(self):
        case = VALID["decode-value.json"]
        edited = copy.deepcopy(case)
        edited["informative"] = {"title": "anything"}
        edited["extension"] = {"expected": "hidden"}
        self.assertEqual(canonical.digest(edited), canonical.digest(case))

    def test_every_normative_change_changes_the_digest(self):
        case = VALID["decode-value.json"]
        for change in (lambda c: c["normative"]["input"].update(octets="3000"),
                       lambda c: c["normative"]["assertions"][0]["clauses"].append("8.1.1"),
                       lambda c: c["normative"]["type"]["fields"].reverse(),
                       lambda c: c["normative"]["standard"].update(rules="ber"),
                       lambda c: c.update(id="example.der.000009")):
            edited = copy.deepcopy(case)
            change(edited)
            self.assertNotEqual(canonical.digest(edited), canonical.digest(case))

    def test_matches_an_independent_serialization_of_ascii_member_names(self):
        # Every member name in a case is ASCII, where RFC 8785 order equals
        # Python's sort order and json.dumps gives the same text.
        for name, case in VALID.items():
            with self.subTest(name):
                definition = canonical.definition(case)
                reference = json.dumps(definition, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                self.assertEqual(canonical.canonicalize(definition), reference)


if __name__ == "__main__":
    unittest.main()
