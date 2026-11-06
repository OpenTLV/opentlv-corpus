# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

from jsonschema import Draft202012Validator

from scripts import canonical, check_adapter, check_contract as contract, protocol

ROOT = contract.ROOT
STUB = [sys.executable, "-B", str(ROOT / "tests" / "stub_adapter.py")]
TRANSCRIPTS = sorted((ROOT / "protocol" / "transcripts").glob("*.jsonl"))
VALID, _ = contract.load_examples()
# Definitions of the request schema that are copies of the test case schema.
COPIED = ("key", "dottedKey", "hex", "unsigned", "signed", "tagClass", "standard", "input", "request", "node", "kind",
          "type", "fieldName", "value")
DER = {"ref": "itu-t-x690-2021-02", "rules": "der"}
FAST = protocol.Limits(hello_timeout=10.0, execute_timeout=10.0, exit_timeout=2.0)


def params(operation="decode", subject=None, view="tree", strictness="strict"):
    return {"standard": dict(DER), "request": {"operation": operation, "strictness": strictness, "view": view},
            "input": subject if subject is not None else {"octets": "020105"}}


def leaf(contents="05"):
    return {"class": "universal", "constructed": False, "number": "2", "contents": contents}


def nested(depth):
    node = leaf()
    for _ in range(depth - 1):
        node = {"class": "universal", "constructed": True, "number": "16", "children": [node]}
    return node


def assertions():
    for name, case in VALID.items():
        for assertion in case["normative"]["assertions"]:
            yield name, case, assertion


def read_transcript(path):
    return [canonical.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


class SchemaTest(unittest.TestCase):
    def test_schemas_are_valid_draft_2020_12_and_versioned(self):
        for kind in protocol.SCHEMA_KINDS:
            schema = canonical.load_json(protocol.PROTOCOL_DIR / f"{kind}-1.schema.json")
            Draft202012Validator.check_schema(schema)
            self.assertEqual(schema["$id"], f"urn:opentlv:corpus:protocol:{kind}:1")

    def test_protocol_files_are_utf8_with_lf_line_endings(self):
        for path in sorted(protocol.PROTOCOL_DIR.rglob("*")):
            if path.is_file():
                with self.subTest(path.name):
                    data = path.read_bytes()
                    data.decode("utf-8")
                    self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                    self.assertNotIn(b"\r", data)
                    self.assertTrue(data.endswith(b"\n"))

    def test_representations_are_copies_of_the_case_schema(self):
        request = canonical.load_json(protocol.PROTOCOL_DIR / "request-1.schema.json")
        case = canonical.load_json(contract.SCHEMAS / "case-1.schema.json")
        for name in COPIED:
            with self.subTest(name):
                self.assertEqual(request["$defs"][name], case["$defs"][name])

    def test_execute_parameters_are_exactly_the_oracle_blind_view(self):
        request = canonical.load_json(protocol.PROTOCOL_DIR / "request-1.schema.json")
        execute = request["$defs"]["executeParams"]
        self.assertEqual(set(execute["properties"]), set(protocol.PARAMS))
        self.assertFalse(execute["additionalProperties"])
        self.assertEqual(contract.EXPECTATION_KEYS & set(execute["properties"]), set())

    def test_every_example_assertion_gives_a_valid_request(self):
        for name, case, assertion in assertions():
            with self.subTest(f"{name}/{assertion['key']}"):
                message = protocol.request("1", "execute", protocol.execute_params(case, assertion["key"]))
                self.assertEqual(protocol.schema_errors(protocol.REQUEST, message), [])

    def test_requests_cannot_carry_answers_or_case_metadata(self):
        case = VALID["decode-tree.json"]
        assertion = case["normative"]["assertions"][0]
        for member, value in (("expect", assertion["expect"]), ("clauses", assertion["clauses"]),
                              ("requires", ["tag-number.over-31-bits"]), ("check", f"{case['id']}/decode"),
                              ("id", case["id"]), ("informative", case["informative"]), ("verdict", "pass")):
            with self.subTest(member):
                view = dict(protocol.execute_params(case, "decode"), **{member: value})
                message = protocol.request("1", "execute", view)
                self.assertNotEqual(protocol.schema_errors(protocol.REQUEST, message), [])
        message = protocol.request("1", "execute", protocol.execute_params(case, "decode"))
        message["params"]["request"]["expect"] = assertion["expect"]
        self.assertNotEqual(protocol.schema_errors(protocol.REQUEST, message), [])

    def test_hello_and_value_requests(self):
        hello = protocol.request("1", "hello", protocol.hello_params(4096))
        self.assertEqual(protocol.schema_errors(protocol.REQUEST, hello), [])
        for change in ({"max_message_octets": 4095}, {"protocols": []}, {"extra": 1}):
            with self.subTest(change):
                broken = copy.deepcopy(hello)
                broken["params"].update(change)
                self.assertNotEqual(protocol.schema_errors(protocol.REQUEST, broken), [])
        value = protocol.request("1", "execute", params(view="value"))
        self.assertEqual(protocol.schema_errors(f"{protocol.REQUEST}#/$defs/executeRequest", value),
                         ["/params (required)"])

    def test_encoded_messages_are_single_ascii_lines(self):
        message = protocol.request("1", "execute", params("encode", {"value": {"kind": "utf8_string", "value": "é\n"}},
                                                          view="value"))
        line = protocol.encode(message)
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(line.count(b"\n"), 1)
        line.decode("ascii")
        self.assertEqual(canonical.loads(line.decode("ascii")), message)


class ClassifyTest(unittest.TestCase):
    """Answers the harness classifies without running an adapter."""

    ACCEPT = {"id": "2", "result": {"outcome": "accept", "tree": leaf()}}

    def classify(self, answer, method="execute", request_id="2", request=None):
        line = answer if isinstance(answer, bytes) else json.dumps(answer).encode("utf-8")
        status, problem, _, _ = protocol.classify(line, method, request_id, request or params())
        return status, problem

    def test_valid_answers(self):
        self.assertEqual(self.classify(self.ACCEPT), ("completed", None))
        self.assertEqual(self.classify(json.dumps(self.ACCEPT, indent=None).encode() + b"\r"), ("completed", None))
        self.assertEqual(self.classify(b' {"result": {"tree": {"contents": "05", "number": "2", "constructed": false, '
                                       b'"class": "universal"}, "outcome": "accept"}, "id": "2"}\t'),
                         ("completed", None))
        reject = {"id": "2", "result": {"outcome": "reject", "reason": "input.truncated",
                                        "diagnostic": {"code": "E_SHORT", "message": "short"}}}
        self.assertEqual(self.classify(reject), ("completed", None))
        unmapped = {"id": "2", "result": {"outcome": "reject", "diagnostic": {"message": "short"}}}
        self.assertEqual(self.classify(unmapped), ("completed", None))
        _, _, _, message = protocol.classify(json.dumps(reject).encode(), "execute", "2", params())
        self.assertEqual(message, reject)

    def test_error_answers(self):
        for code, status in (("bad-request", "bad-request"), ("unsupported", "unsupported"),
                             ("not-comparable", "not-comparable"), ("internal-error", "crashed")):
            with self.subTest(code):
                answer = {"id": "2", "error": {"code": code, "message": "m"}}
                self.assertEqual(self.classify(answer), (status, code))
        self.assertEqual(self.classify({"id": None, "error": {"code": "bad-request", "message": "m"}}, request_id=None),
                         ("bad-request", "bad-request"))
        version = {"id": "1", "error": {"code": "version-unsupported", "message": "m"}}
        self.assertEqual(self.classify(version, "hello", "1"), ("unsupported", "version-unsupported"))

    def test_malformed_lines(self):
        valid = json.dumps(self.ACCEPT).encode()
        cases = {
            "invalid UTF-8": (b'{"id": "2\xff"}', "encoding-invalid"),
            "empty line": (b"", "json-invalid"),
            "blank line": (b" \r", "json-invalid"),
            "not JSON": (b"starting", "json-invalid"),
            "truncated": (valid[:-1], "json-invalid"),
            "byte order mark": (b"\xef\xbb\xbf" + valid, "json-invalid"),
            "repeated member": (b'{"id": "2", "id": "2", "result": {"outcome": "accept", "octets": ""}}',
                                "json-invalid"),
            "NaN": (b'{"id": "2", "result": {"outcome": "accept", "octets": NaN}}', "json-invalid"),
            "two messages": (valid + b" " + valid, "json-invalid"),
            "array": (b"[]", "schema"),
            "string": (b'"ok"', "schema"),
        }
        for name, (line, problem) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(line), ("protocol-error", problem))

    def test_answers_that_break_the_schema(self):
        result = self.ACCEPT["result"]
        cases = {
            "unknown member": dict(self.ACCEPT, extra=1),
            "missing id": {"result": result},
            "result and error": dict(self.ACCEPT, error={"code": "unsupported", "message": "m"}),
            "neither result nor error": {"id": "2"},
            "numeric id": dict(self.ACCEPT, id=2),
            "unknown error code": {"id": "2", "error": {"code": "crashed", "message": "m"}},
            "error without message": {"id": "2", "error": {"code": "unsupported"}},
            "long error message": {"id": "2", "error": {"code": "unsupported", "message": "m" * 4097}},
            "null id outside bad-request": {"id": None, "error": {"code": "unsupported", "message": "m"}},
            "accept with a reason": {"id": "2", "result": dict(result, reason="input")},
            "accept without output": {"id": "2", "result": {"outcome": "accept"}},
            "accept with two outputs": {"id": "2", "result": dict(result, octets="00")},
            "reject with output": {"id": "2", "result": {"outcome": "reject", "tree": leaf()}},
            "unknown outcome": {"id": "2", "result": {"outcome": "pass"}},
            "verdict in the result": {"id": "2", "result": dict(result, verdict="pass")},
            "native reason code": {"id": "2", "result": {"outcome": "reject", "reason": "E_SHORT"}},
            "empty diagnostic": {"id": "2", "result": {"outcome": "reject", "diagnostic": {}}},
            "uppercase hex": {"id": "2", "result": {"outcome": "accept", "octets": "0A"}},
            "numeric tag number": {"id": "2", "result": {"outcome": "accept", "tree": dict(leaf(), number=2)}},
            "leading zero tag number": {"id": "2", "result": {"outcome": "accept", "tree": dict(leaf(), number="02")}},
            "step with two outputs": {"id": "2", "result": {"outcome": "accept",
                                                            "steps": [{"octets": "", "tree": leaf()}]}},
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(answer), ("protocol-error", "schema"))

    def test_answers_that_do_not_fit_the_request(self):
        tree = {"tree": leaf()}
        octets = {"octets": "020105"}
        round_trip = params("round-trip")
        tree_round_trip = params("round-trip", {"tree": leaf()})
        cases = {
            "another id": ({"id": "3", "result": {"outcome": "accept", "tree": leaf()}}, params(), "id-mismatch"),
            "null id to a valid request": ({"id": None, "error": {"code": "bad-request", "message": "m"}}, params(),
                                           "id-mismatch"),
            "decode yields octets": ({"id": "2", "result": {"outcome": "accept", "octets": "00"}}, params(),
                                     "answer-mismatch"),
            "decode in the value view yields a tree": ({"id": "2", "result": {"outcome": "accept", "tree": leaf()}},
                                                       params(view="value"), "answer-mismatch"),
            "encode yields a tree": ({"id": "2", "result": {"outcome": "accept", "tree": leaf()}},
                                     params("encode", {"tree": leaf()}), "answer-mismatch"),
            "decode with steps": ({"id": "2", "result": {"outcome": "reject", "steps": []}}, params(),
                                  "answer-mismatch"),
            "short round trip": ({"id": "2", "result": {"outcome": "accept", "steps": [tree, octets]}}, round_trip,
                                 "answer-mismatch"),
            "round trip out of order": ({"id": "2", "result": {"outcome": "accept", "steps": [tree, octets]}},
                                        tree_round_trip, "answer-mismatch"),
            "rejected round trip with every step": ({"id": "2", "result": {"outcome": "reject", "steps": [tree, octets,
                                                                                                          tree]}},
                                                    round_trip, "answer-mismatch"),
            "rejected round trip with a wrong step": ({"id": "2", "result": {"outcome": "reject", "steps": [octets]}},
                                                      round_trip, "answer-mismatch"),
            "execute answers version-unsupported": ({"id": "2", "error": {"code": "version-unsupported",
                                                                          "message": "m"}}, params(),
                                                    "answer-mismatch"),
        }
        for name, (answer, request, problem) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.classify(answer, request=request), ("protocol-error", problem))
        for steps in ([], [tree], [tree, octets]):
            answer = {"id": "2", "result": {"outcome": "reject", "reason": "input", "steps": steps}}
            self.assertEqual(self.classify(answer, request=round_trip), ("completed", None))
        answer = {"id": "2", "result": {"outcome": "accept", "steps": [octets, tree]}}
        self.assertEqual(self.classify(answer, request=tree_round_trip), ("completed", None))

    def test_hello_answers(self):
        stub = read_transcript(ROOT / "protocol" / "transcripts" / "session.jsonl")[2]["receive"]
        self.assertEqual(self.classify(stub, "hello", "1"), ("completed", None))
        for change in ({"protocol": 2}, {"supports": []}, {"provides": ["x", "x"]}, {"limits": {}},
                       {"adapter": {"name": "a"}}, {"extra": True}):
            with self.subTest(change):
                answer = copy.deepcopy(stub)
                answer["result"].update(change)
                self.assertEqual(self.classify(answer, "hello", "1"), ("protocol-error", "schema"))
        for code in ("unsupported", "not-comparable"):
            answer = {"id": "1", "error": {"code": code, "message": "m"}}
            self.assertEqual(self.classify(answer, "hello", "1"), ("protocol-error", "answer-mismatch"))
        execute_answer = {"id": "1", "result": {"outcome": "accept", "tree": leaf()}}
        self.assertEqual(self.classify(execute_answer, "hello", "1"), ("protocol-error", "schema"))


class ApplicabilityTest(unittest.TestCase):
    DESCRIPTOR = read_transcript(ROOT / "protocol" / "transcripts" / "session.jsonl")[2]["receive"]["result"]

    def decide(self, request, requires=()):
        decision = protocol.applicability(self.DESCRIPTOR, request, requires)
        return decision and decision[0]

    def test_descriptor_coverage(self):
        self.assertIsNone(self.decide(params()))
        self.assertIsNone(self.decide(params("round-trip", {"tree": nested(32)})))
        self.assertEqual(self.decide(params(view="value")), "undeclared")
        self.assertEqual(self.decide(params(strictness="lenient")), "undeclared")
        self.assertEqual(self.decide(dict(params(), standard={"ref": "itu-t-x690-2021-02", "rules": "ber"})),
                         "undeclared")
        self.assertEqual(self.decide(params(), ["tag-number.over-31-bits"]), "capability")
        self.assertEqual(self.decide(params(subject={"octets": "00" * 65537})), "input-too-large")
        self.assertIsNone(self.decide(params(subject={"octets": "00" * 65536})))
        self.assertEqual(self.decide(params("encode", {"tree": nested(33)})), "too-deep")

    def test_depth_of_values(self):
        integer = {"kind": "integer", "value": "1"}
        self.assertEqual(protocol.depth(integer), 1)
        self.assertEqual(protocol.depth({"kind": "sequence", "fields": []}), 1)
        self.assertEqual(protocol.depth({"kind": "sequence", "fields": [{"name": "a", "value": integer}]}), 2)
        self.assertEqual(protocol.depth({"kind": "set_of", "items": [{"kind": "choice", "alternative": "a",
                                                                      "value": integer}]}), 3)
        self.assertEqual(protocol.depth(nested(5)), 5)


class TranscriptTest(unittest.TestCase):
    def test_transcripts_exist(self):
        self.assertEqual([path.stem for path in TRANSCRIPTS], ["bad-requests", "session", "version-unsupported"])

    def test_transcripts_are_well_formed(self):
        for path in TRANSCRIPTS:
            with self.subTest(path.name):
                data = path.read_bytes()
                self.assertTrue(data.endswith(b"\n"))
                data.decode("ascii")
                events = read_transcript(path)
                self.assertEqual(set(events[0]), {"title", "stub_arguments"})
                self.assertEqual(events[-2:], [{"close": True}, {"exit": 0}])
                ids = []
                for event, answer in zip(events[1:-2:2], events[2:-2:2]):
                    self.assertEqual(set(answer), {"receive", "status"})
                    self.assertIn(answer["status"], protocol.STATUSES)
                    if "send" in event:
                        self.assertEqual(set(event), {"send"})
                        self.assertEqual(protocol.schema_errors(protocol.REQUEST, event["send"]), [])
                        ids.append(event["send"]["id"])
                    else:
                        self.assertEqual(set(event), {"send_raw", "method", "id"})
                        self.assertNotIn("\n", event["send_raw"])
                    self.assertEqual(protocol.schema_errors(protocol.RESPONSE, answer["receive"]), [])
                self.assertEqual(len(ids), len(set(ids)))

    def test_transcripts_replay_against_the_stub(self):
        for path in TRANSCRIPTS:
            with self.subTest(path.name):
                events = read_transcript(path)
                channel = protocol.Channel(STUB + events[0]["stub_arguments"], 1 << 22, 1 << 16)
                self.addCleanup(channel.kill)
                pending = None
                for event in events[1:]:
                    if "send" in event:
                        message = event["send"]
                        channel.write(protocol.encode(message))
                        pending = message["method"], message["id"], message["params"]
                    elif "send_raw" in event:
                        channel.write(event["send_raw"].encode("utf-8") + b"\n")
                        pending = event["method"], event["id"], None
                    elif "receive" in event:
                        kind, line = channel.next_event(10)
                        self.assertEqual(kind, "line")
                        status, _, _, message = protocol.classify(line, *pending)
                        self.assertEqual((message, status), (event["receive"], event["status"]))
                    elif "close" in event:
                        channel.close_input()
                    else:
                        self.assertEqual(channel.wait(10), event["exit"])
                        self.assertEqual(channel.next_event(10)[0], "eof")


class SessionTestCase(unittest.TestCase):
    def session(self, *arguments, limits=FAST):
        session = protocol.Session(STUB + list(arguments), limits)
        self.addCleanup(session.close)
        return session

    def started(self, *arguments, limits=FAST):
        session = self.session(*arguments, limits=limits)
        session.start()
        return session


class SessionTest(SessionTestCase):
    def test_complete_session(self):
        session = self.session("--stderr")
        hello = session.start()
        self.assertEqual(hello.status, "completed")
        self.assertEqual(session.descriptor["adapter"]["name"], "opentlv-corpus-stub-adapter")
        exchange = session.execute(params())
        self.assertEqual((exchange.id, exchange.status), ("2", "completed"))
        self.assertEqual(exchange.response["result"], {"outcome": "accept", "tree": leaf()})
        # Standard error is read concurrently, so its text may trail the answer.
        stderr = [exchange.stderr]
        self.assertTrue(wait_for(lambda: stderr.append(session._channel.take_stderr())
                                 or "stub: answered 2" in "".join(stderr)))
        self.assertEqual(session.close(), 0)
        self.assertEqual((session.events, session.restarts), ([], 0))

    def test_execute_needs_a_handshake(self):
        with self.assertRaises(protocol.SessionError) as raised:
            self.session().execute(params())
        self.assertEqual(raised.exception.code, "not-started")

    def test_the_harness_sends_nothing_the_descriptor_does_not_cover(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "record"
            session = self.started("--record", str(record))
            huge_tree = {"class": "universal", "constructed": False, "number": "4", "contents": "00" * 600000}
            decisions = [
                (session.execute(params(view="value")), "undeclared"),
                (session.execute(params(strictness="lenient")), "undeclared"),
                (session.execute(params(), ["tag-number.over-31-bits"]), "capability"),
                (session.execute(params(subject={"octets": "00" * 65537})), "input-too-large"),
                (session.execute(params("encode", {"tree": nested(33)})), "too-deep"),
                (session.execute(params("encode", {"tree": huge_tree})), "message-too-large"),
            ]
            for exchange, problem in decisions:
                self.assertEqual((exchange.id, exchange.status, exchange.problem), (None, "unsupported", problem))
            session.close()
            self.assertEqual(len(record.read_bytes().splitlines()), 1)

    def test_adapter_decides_unsupported(self):
        session = self.started()
        deep = "0500"
        for _ in range(32):
            deep = f"30{len(deep) // 2:02x}{deep}"
        exchange = session.execute(params(subject={"octets": deep}))
        self.assertEqual((exchange.status, exchange.problem), ("unsupported", "unsupported"))
        self.assertIsNotNone(exchange.id)
        self.assertTrue(session.running)
        self.assertEqual(session.execute(params()).status, "completed")
        self.assertEqual(session.restarts, 0)

    def test_requests_are_idempotent(self):
        session = self.started()
        for request in (params(), params("round-trip", {"octets": "3003020105"})):
            first, second = session.execute(request), session.execute(request)
            self.assertEqual(first.status, "completed")
            self.assertEqual(first.response["result"], second.response["result"])


class FailureTest(SessionTestCase):
    """Each failure is classified, attributed to its request and followed by a working restart."""

    def assertFailure(self, mode, status, problem, limits=FAST, restarts=1):
        # A failure mode acts on the Nth request of each process, so the
        # restarted process answers the request after the failure.
        session = self.started("--fail", f"{mode}@2", limits=limits)
        self.assertEqual(session.execute(params()).status, "completed")
        exchange = session.execute(params())
        self.assertEqual((exchange.status, exchange.problem), (status, problem))
        if restarts:
            self.assertIsNone(exchange.response)
        after = session.execute(params())
        self.assertEqual(after.status, "completed")
        self.assertEqual(session.restarts, restarts)
        return exchange

    def test_crash(self):
        exchange = self.assertFailure("crash", "crashed", "exited")
        self.assertEqual(exchange.exit_status, 3)
        self.assertIn("stub: simulated crash", exchange.stderr)

    def test_timeout(self):
        exchange = self.assertFailure("hang", "timeout", None, limits=protocol.Limits(execute_timeout=0.5))
        self.assertIsNotNone(exchange.exit_status)

    def test_malformed_output(self):
        self.assertFailure("garbage", "protocol-error", "json-invalid")
        self.assertFailure("noise", "protocol-error", "json-invalid")
        self.assertFailure("wrong-id", "protocol-error", "id-mismatch")

    def test_oversized_answer(self):
        limits = protocol.Limits(max_message_octets=4096)
        self.assertFailure("huge", "protocol-error", "message-too-large", limits=limits)

    def test_answered_failures_keep_the_process(self):
        exchange = self.assertFailure("internal", "crashed", "internal-error", restarts=0)
        self.assertEqual(exchange.detail, "simulated failure")
        self.assertEqual(exchange.response["error"]["code"], "internal-error")
        self.assertFailure("not-comparable", "not-comparable", "not-comparable", restarts=0)

    def test_failed_requests_are_never_resent(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "record"
            session = self.started("--fail", "crash@2", "--record", str(record))
            statuses = [session.execute(params()).status for _ in range(4)]
            session.close()
            self.assertEqual(statuses, ["completed", "crashed", "completed", "crashed"])
            ids = [json.loads(line)["id"] for line in record.read_bytes().splitlines()]
            self.assertEqual(ids, ["1", "2", "3", "4", "5", "6"])
            methods = [json.loads(line)["method"] for line in record.read_bytes().splitlines()]
            self.assertEqual(methods, ["hello", "execute", "execute", "hello", "execute", "execute"])
            self.assertEqual(session.restarts, 1)

    def test_unsolicited_output_restarts_before_the_next_request(self):
        session = self.started("--fail", "unsolicited@1")
        self.assertEqual(session.execute(params()).status, "completed")
        self.assertTrue(wait_for(session._channel.pending))
        self.assertEqual(session.execute(params()).status, "completed")
        self.assertEqual([code for code, _ in session.events], ["unsolicited-output", "restart"])

    def test_exit_between_requests_restarts_before_the_next_request(self):
        session = self.started("--fail", "quit@1")
        self.assertEqual(session.execute(params()).status, "completed")
        self.assertTrue(wait_for(lambda: not session.running))
        self.assertEqual(session.execute(params()).status, "completed")
        self.assertEqual([code for code, _ in session.events], ["exited", "restart"])


class StartTest(SessionTestCase):
    def assertStartFails(self, session, code, status, problem):
        with self.assertRaises(protocol.SessionError) as raised:
            session.start()
        error = raised.exception
        self.assertEqual((error.code, error.exchange.status, error.exchange.problem), (code, status, problem))
        self.assertFalse(session.running)

    def test_crash_at_hello(self):
        self.assertStartFails(self.session("--fail", "crash@hello"), "start-failed", "crashed", "exited")

    def test_hang_at_hello(self):
        session = self.session("--fail", "hang@hello", limits=protocol.Limits(hello_timeout=0.5))
        self.assertStartFails(session, "start-failed", "timeout", None)

    def test_no_common_version(self):
        self.assertStartFails(self.session("--protocols", "2"), "start-failed", "unsupported", "version-unsupported")

    def test_adapter_that_cannot_start(self):
        session = protocol.Session([str(ROOT / "no-such-adapter")], FAST)
        self.assertStartFails(session, "start-failed", "harness-error", "spawn-failed")

    def test_a_restarted_adapter_must_be_the_same(self):
        with tempfile.TemporaryDirectory() as directory:
            session = self.started("--launch-file", str(Path(directory) / "launches"), "--vary-identity",
                                   "--fail", "crash@1")
            self.assertEqual(session.execute(params()).status, "crashed")
            with self.assertRaises(protocol.SessionError) as raised:
                session.execute(params())
            self.assertEqual(raised.exception.code, "adapter-changed")
            self.assertFalse(session.running)

    def test_shutdown(self):
        session = self.started("--exit-status", "4")
        self.assertEqual(session.close(), 4)
        self.assertEqual(session.events, [("exit-status", "4")])
        session = self.started("--ignore-eof", limits=protocol.Limits(exit_timeout=0.5))
        self.assertNotEqual(session.close(), 0)
        self.assertEqual([code for code, _ in session.events], ["exit-timeout"])
        self.assertIsNone(session.close())


class OracleBlindTest(SessionTestCase):
    def test_adapters_receive_only_oracle_blind_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "record"
            session = self.started("--record", str(record))
            sent = {}
            for name, case, assertion in assertions():
                exchange = session.execute(protocol.execute_params(case, assertion["key"]),
                                           assertion.get("requires", ()))
                self.assertIn(exchange.status, ("completed", "unsupported"))
                if exchange.id is not None:
                    sent[exchange.id] = case, assertion
            session.close()
            lines = record.read_bytes().splitlines()
            self.assertGreater(len(sent), 5)
            self.assertEqual(len(lines), len(sent) + 1)
            for line in lines[1:]:
                message = canonical.loads(line.decode("ascii"))
                case, assertion = sent[message["id"]]
                with self.subTest(f"{case['id']}/{assertion['key']}"):
                    self.assertEqual(protocol.schema_errors(protocol.REQUEST, message), [])
                    self.assertLessEqual(set(message["params"]), set(protocol.PARAMS))
                    self.assertEqual(contract.expectation_keys(message), set())
                    for secret in (case["id"], *assertion["clauses"], case["informative"]["title"],
                                   case["informative"]["oracle"]["rationale"],
                                   json.dumps(assertion["expect"], separators=(",", ":"))):
                        self.assertNotIn(secret.encode("ascii", "backslashreplace"), line)

    def test_every_example_request_is_oracle_blind_on_the_wire(self):
        for name, case, assertion in assertions():
            with self.subTest(f"{name}/{assertion['key']}"):
                view = protocol.execute_params(case, assertion["key"])
                line = protocol.encode(protocol.request("1", "execute", view))
                self.assertNotIn(case["id"].encode(), line)
                self.assertNotIn(b'"expect', line)
                self.assertNotIn(b'"clauses"', line)
                self.assertNotIn(b'"requires"', line)


class CheckAdapterTest(unittest.TestCase):
    def test_the_stub_conforms(self):
        problems, descriptor, counts = check_adapter.check(STUB, FAST)
        self.assertEqual(problems, [])
        self.assertEqual(descriptor["implementation"]["name"], "stub-der-tree")
        self.assertEqual(set(counts), {"completed", "unsupported"})
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(check_adapter.main(["--", *STUB]), 0)
        self.assertTrue(stdout.getvalue().startswith("adapter ok: opentlv-corpus-stub-adapter 1.0.0"))

    def test_protocol_violations_are_reported(self):
        problems, _, _ = check_adapter.check(STUB + ["--fail", "wrong-id@1"], FAST)
        self.assertTrue(problems)
        self.assertTrue(all(problem.split(": ")[1] == "protocol-error" for problem in problems), problems)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(check_adapter.main(["--", *STUB, "--fail", "wrong-id@1"]), 1)
        self.assertIn("adapter nonconforming", stderr.getvalue())

    def test_implementation_failures_are_counted_not_reported(self):
        problems, _, counts = check_adapter.check(STUB + ["--fail", "crash@1"], FAST)
        self.assertEqual(problems, [])
        self.assertGreater(counts["crashed"], 0)

    def test_an_adapter_that_does_not_start(self):
        problems, descriptor, _ = check_adapter.check(STUB + ["--protocols", "2"], FAST)
        self.assertIsNone(descriptor)
        self.assertEqual(problems, ["session: start-failed: hello ended with unsupported"])


if __name__ == "__main__":
    unittest.main()
