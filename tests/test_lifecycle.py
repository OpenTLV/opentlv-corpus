# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import copy
import json
import random
import shutil
import subprocess
import unittest

from jsonschema import Draft202012Validator

from scripts import canonical, check_contract, check_history, check_lifecycle as lifecycle

LOG, CASES = lifecycle.load_set("examples")
BY_ID = {case["id"]: case for case in CASES}
REVIEW = {"status": "self-reviewed", "reviewers": ["Marek Cingel"], "date": "2026-11-06"}
OLD_DIGEST = "sha256:" + "1" * 64


def digest(case_id, cases=CASES):
    return canonical.digest(next(case for case in cases if case["id"] == case_id))


def event(case_id, kind, code, cases=CASES, **extra):
    item = {"seq": 0, "case": case_id, "digest": extra.pop("digest", None) or digest(case_id, cases), "event": kind,
            "reason": {"code": code, "detail": "x"}, "actor": "Marek Cingel", "review": REVIEW}
    item.update(extra)
    return item


def appended(log, *events):
    result = copy.deepcopy(log)
    for item in events:
        result["events"].append(dict(item, seq=len(result["events"]) + 1))
    return result


def codes(problems):
    return sorted({code for code, _ in problems})


def revised(case_id, octets):
    """Return the example cases with one input changed in place."""
    cases = copy.deepcopy(CASES)
    next(case for case in cases if case["id"] == case_id)["normative"]["input"]["octets"] = octets
    return cases


class LogTest(unittest.TestCase):
    def test_schema_is_valid_draft_2020_12_and_versioned(self):
        schema = check_contract.load_json(check_contract.SCHEMAS / "lifecycle-1.schema.json")
        Draft202012Validator.check_schema(schema)
        self.assertEqual(schema["$id"], "urn:opentlv:corpus:schema:lifecycle:1")
        self.assertEqual(schema["properties"]["schema"], {"const": schema["$id"]})
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(schema["$defs"]["event"]["additionalProperties"])

    def test_committed_logs_projection_and_vectors_satisfy_the_contract(self):
        self.assertEqual(lifecycle.run(), [])

    def test_conformance_log_starts_empty_in_0x(self):
        log, cases = lifecycle.load_set("conformance")
        self.assertEqual((log["phase"], log["events"]), ("0.x", []))
        self.assertEqual(lifecycle.check_log(log, cases), ("semantic", []))

    def test_every_invalid_log_breaks_exactly_its_declared_rule(self):
        invalid = lifecycle.load_invalid_logs(LOG)
        names = [entry["name"] for entry, _ in invalid]
        self.assertEqual(len(names), len(set(names)))
        for entry, document in invalid:
            with self.subTest(entry["name"]):
                ((stage, expected),) = entry["violation"].items()
                self.assertEqual(lifecycle.check_log(document, CASES), (stage, sorted(expected)))

    def test_invalid_logs_cover_every_semantic_rule(self):
        found = {code for entry, _ in lifecycle.load_invalid_logs(LOG) for code in entry["violation"].get("semantic", ())}
        self.assertEqual(found, {"event-sequence", "event-case-unknown", "event-digest-mismatch", "transition-forbidden",
                                 "replacement-unknown", "replacement-cycle", "replacement-inactive",
                                 "revision-after-stable", "revision-unchanged", "review-status-mismatch",
                                 "review-not-independent"})


class ProjectionTest(unittest.TestCase):
    def test_example_projection(self):
        projection, problems = lifecycle.replay(LOG, CASES)
        self.assertEqual(problems, [])
        cases = {item["id"]: item for item in projection["cases"]}
        self.assertEqual(len(cases), len(CASES))
        self.assertEqual(cases["example.der.000009"]["status"], "deprecated")
        self.assertEqual(cases["example.der.000009"]["replacement"], "example.der.000001")
        self.assertEqual(cases["example.der.000007"]["status"], "invalid")
        self.assertEqual(cases["example.der.000008"], {"id": "example.der.000008", "status": "active",
                                                       "digest": digest("example.der.000008")})
        self.assertEqual(projection["tainted"], [{"case": "example.der.000007", "digest": digest("example.der.000007"),
                                                  "event": 2}])

    def test_projection_is_reproducible(self):
        expected = canonical.canonical_bytes(lifecycle.replay(LOG, CASES)[0])
        shuffled = json.loads(json.dumps(CASES))
        random.Random(3).shuffle(shuffled)
        self.assertEqual(canonical.canonical_bytes(lifecycle.replay(json.loads(json.dumps(LOG)), shuffled)[0]),
                         expected)
        self.assertEqual(lifecycle.replay(LOG, CASES)[0], check_contract.load_json(lifecycle.PROJECTION))

    def test_deprecation_can_be_withdrawn_and_then_invalidated(self):
        log = appended(LOG, event("example.der.000009", "reinstate", "deprecation-withdrawn"),
                       event("example.der.000009", "invalidate", "definition-wrong"))
        projection, problems = lifecycle.replay(log, CASES)
        self.assertEqual(problems, [])
        item = next(item for item in projection["cases"] if item["id"] == "example.der.000009")
        self.assertEqual((item["status"], item.get("replacement"), item["events"]), ("invalid", None, [1, 3, 4]))

    def test_replacement_chains_survive_a_later_invalidation(self):
        # 000007 is replaced by 000008; when 000008 is invalidated in turn it names
        # its own replacement, so the chain from 000007 still ends at an active case.
        log = appended(LOG, event("example.der.000008", "invalidate", "expectation-wrong",
                                  replacement="example.der.000004"))
        projection, problems = lifecycle.replay(log, CASES)
        self.assertEqual(problems, [])
        self.assertEqual([item["case"] for item in projection["tainted"]], ["example.der.000007", "example.der.000008"])

    def test_revision_in_0x_taints_the_previous_definition(self):
        cases = revised("example.der.000003", "020200ff")
        log = appended(LOG, event("example.der.000003", "revise", "expectation-wrong", digest=digest("example.der.000003"),
                                  revised_digest=digest("example.der.000003", cases)))
        projection, problems = lifecycle.replay(log, cases)
        self.assertEqual(problems, [])
        item = next(item for item in projection["cases"] if item["id"] == "example.der.000003")
        self.assertEqual((item["status"], item["digest"]), ("active", digest("example.der.000003", cases)))
        self.assertIn({"case": "example.der.000003", "digest": digest("example.der.000003"), "event": 3},
                      projection["tainted"])

    def test_representation_revisions_are_allowed_after_stable_and_taint_nothing(self):
        cases = revised("example.der.000003", "020200ff")
        log = appended(dict(LOG, phase="stable"),
                       event("example.der.000003", "revise", "representation", digest=digest("example.der.000003"),
                             revised_digest=digest("example.der.000003", cases)))
        projection, problems = lifecycle.replay(log, cases)
        self.assertEqual(problems, [])
        self.assertEqual(len(projection["tainted"]), 1)

    def test_a_definition_changed_without_a_revision_is_detected(self):
        cases = revised("example.der.000007", "010102")
        self.assertEqual(codes(lifecycle.replay(LOG, cases)[1]), ["event-digest-mismatch"])

    def test_revision_chain_must_match_the_current_definition(self):
        cases = revised("example.der.000003", "020200ff")
        log = appended(LOG, event("example.der.000003", "revise", "expectation-wrong", digest=digest("example.der.000003"),
                                  revised_digest=OLD_DIGEST))
        self.assertEqual(codes(lifecycle.replay(log, cases)[1]), ["event-digest-mismatch"])

    def test_duplicate_case_ids_are_reported(self):
        self.assertEqual(codes(lifecycle.replay(LOG, CASES + [copy.deepcopy(CASES[0])])[1]), ["duplicate-id"])


class TransitionTest(unittest.TestCase):
    def check(self, new_log, new_cases=CASES, old_log=LOG, old_cases=CASES):
        return codes(lifecycle.check_transition(old_log, new_log, old_cases, new_cases))

    def test_appending_and_metadata_edits_are_allowed(self):
        self.assertEqual(self.check(LOG), [])
        self.assertEqual(self.check(appended(LOG, event("example.der.000002", "deprecate", "out-of-scope"))), [])
        edited = copy.deepcopy(CASES)
        edited[0]["informative"]["title"] = "Retitled"
        self.assertEqual(self.check(LOG, edited), [])
        self.assertEqual(self.check(LOG, old_log=None), [])
        self.assertEqual(self.check(None, old_log=None), [])

    def test_new_cases_can_be_added(self):
        new = copy.deepcopy(BY_ID["example.der.000001"])
        new["id"] = "example.der.000010"
        self.assertEqual(self.check(LOG, CASES + [new]), [])

    def test_the_log_only_grows(self):
        removed = copy.deepcopy(LOG)
        removed["events"].pop()
        self.assertEqual(self.check(removed), ["event-removed"])
        rewritten = copy.deepcopy(LOG)
        rewritten["events"][0]["reason"]["detail"] = "Reworded"
        self.assertEqual(self.check(rewritten), ["event-rewritten"])
        self.assertEqual(self.check(None), ["log-removed"])

    def test_stable_phase_is_permanent(self):
        stable = dict(LOG, phase="stable")
        self.assertEqual(self.check(stable), [])
        self.assertEqual(self.check(LOG, old_log=stable), ["phase-reverted"])

    def test_definitions_are_never_deleted(self):
        remaining = [case for case in CASES if case["id"] != "example.der.000009"]
        self.assertIn("case-removed", self.check(LOG, remaining))

    def test_a_changed_definition_needs_a_revise_event(self):
        cases = revised("example.der.000003", "020200ff")
        self.assertEqual(self.check(LOG, cases), ["definition-rewritten"])
        revision = event("example.der.000003", "revise", "expectation-wrong", digest=digest("example.der.000003"),
                         revised_digest=digest("example.der.000003", cases))
        self.assertEqual(self.check(appended(LOG, revision), cases), [])
        deprecation = event("example.der.000003", "deprecate", "out-of-scope", cases=cases)
        self.assertEqual(self.check(appended(LOG, deprecation), cases),
                         ["definition-rewritten", "event-digest-mismatch"])

    def test_appended_events_start_from_the_base_definition(self):
        cases = revised("example.der.000003", "020200ff")
        revision = event("example.der.000003", "revise", "expectation-wrong", digest=OLD_DIGEST,
                         revised_digest=digest("example.der.000003", cases))
        self.assertEqual(self.check(appended(LOG, revision), cases), ["event-digest-mismatch"])


@unittest.skipUnless(shutil.which("git") and subprocess.run(["git", "rev-parse", "HEAD"], cwd=check_contract.ROOT,
                                                            capture_output=True).returncode == 0,
                     "needs a Git checkout")
class HistoryTest(unittest.TestCase):
    def test_reads_documents_at_a_revision(self):
        base = check_history.Revision("HEAD")
        self.assertEqual(base.load(check_history.REGISTRY)["schema"], "urn:opentlv:corpus:schema:registry:1")
        self.assertIsNone(base.load("registry/missing.json"))
        self.assertTrue(all(case["schema"] == "urn:opentlv:corpus:schema:case:1"
                            for case in base.cases("examples/valid")))

    def test_unknown_revisions_are_refused(self):
        with self.assertRaisesRegex(ValueError, "unknown revision"):
            check_history.Revision("refs/heads/does-not-exist")


if __name__ == "__main__":
    unittest.main()
