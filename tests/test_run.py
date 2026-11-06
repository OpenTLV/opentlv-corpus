# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest

from corpus_fixture import case_path
from scripts import canonical, check_contract, plan, protocol, run
from test_plan import DESCRIPTOR, EXAMPLES, SMOKE, STUB, build, write_fixture

ROOT = check_contract.ROOT
FAILURES = ("protocol-error", "crashed", "timeout", "harness-error", "bad-request", run.NOT_RUN)
# The Core adapter command, when the environment names one; see docs/runner.md.
CORE_ADAPTER = os.environ.get("OPENTLV_CORPUS_CORE_ADAPTER")


def alive(pid):
    """Whether a process with this ID is still running."""
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill(pid):
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if handle:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
                ctypes.windll.kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 9)


def parse(data):
    return [canonical.loads(line.decode("utf-8")) for line in data.splitlines()]


class RunTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.directory = Path(cls._directory.name)
        cls.root = write_fixture(cls.directory / "corpus")
        cls.release, cls.archive = build(cls.root, cls.directory / "release")

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def setUp(self):
        self._work = tempfile.TemporaryDirectory()
        self.work = Path(self._work.name)

    def tearDown(self):
        self._work.cleanup()

    def resolve(self, select, descriptor=DESCRIPTOR, **members):
        document = {"schema": plan.PLAN, "name": "test", **members, "select": select}
        resolved, problems = plan.resolve(plan.load_plan(document), self.release, descriptor)
        self.assertEqual(problems, [])
        return resolved

    def run_plan(self, resolved, *arguments, **options):
        """Run a resolved plan with the stub adapter; return the validated records."""
        stream = io.BytesIO()
        run.run(resolved, self.release, STUB + list(arguments), stream, **options)
        records = parse(stream.getvalue())
        for record in records:
            errors = check_contract.schema_errors("observations", record)
            self.assertEqual([f"{list(error.absolute_path)}: {error.message}" for error in errors], [], record)
        return records

    def checks(self, records):
        return {f"{record['case']}/{record['assertion']}": record for record in records if record["record"] == "check"}

    def events(self, records):
        return [record["event"] for record in records if record["record"] == "session"]


class SchemaTest(unittest.TestCase):
    def test_shared_definitions_match_the_resolved_plan(self):
        observations = canonical.load_json(ROOT / "schemas" / "observations-1.schema.json")
        resolution = canonical.load_json(ROOT / "schemas" / "resolution-1.schema.json")
        for name in ("key", "version", "digest", "milliseconds", "component"):
            self.assertEqual(observations["$defs"][name], resolution["$defs"][name], name)
        run_record = observations["$defs"]["run"]["properties"]
        for name in ("corpus", "limits"):
            self.assertEqual({key: value for key, value in run_record[name].items() if key != "description"},
                             {key: value for key, value in resolution["properties"][name].items()
                              if key != "description"}, name)
        check = observations["$defs"]["check"]["properties"]
        self.assertEqual(check["case"], resolution["$defs"]["check"]["properties"]["case"])
        self.assertEqual(check["status"]["enum"], resolution["$defs"]["check"]["properties"]["status"]["enum"])

    def test_execution_statuses_are_the_protocol_statuses(self):
        observations = canonical.load_json(ROOT / "schemas" / "observations-1.schema.json")
        self.assertEqual(observations["$defs"]["execution"]["enum"], list(protocol.STATUSES) + [run.NOT_RUN])

    def test_a_result_needs_a_completed_exchange(self):
        check = {"record": "check", "case": "asn1.der.000001", "assertion": "decode",
                 "digest": "sha256:" + "0" * 64, "status": "active"}
        valid = (dict(check, execution="completed", request="2", result={"outcome": "accept", "octets": "00"}),
                 dict(check, execution="unsupported", problem="undeclared", detail="not declared"),
                 dict(check, execution="not-run", problem="session-ended", detail="the session ended"))
        invalid = (dict(check, execution="completed", request="2"),
                   dict(check, execution="crashed", problem="exited", detail="ended", request="2",
                        result={"outcome": "accept", "octets": "00"}),
                   dict(check, execution="not-run", problem="session-ended", detail="ended", request="2"),
                   dict(check, execution="timeout"))
        for record in valid:
            self.assertEqual(check_contract.schema_errors("observations", record), [], record)
        for record in invalid:
            self.assertNotEqual(check_contract.schema_errors("observations", record), [], record)


class RunTest(RunTestCase):
    def test_smoke_plan(self):
        resolved = self.resolve(EXAMPLES["smoke.json"]["select"])
        records = self.run_plan(resolved)
        self.assertEqual([record["record"] for record in records],
                         ["run"] + ["check"] * len(resolved.checks) + ["end"])
        checks = self.checks(records)
        self.assertEqual(list(checks), [check.id for check in resolved.checks])
        self.assertEqual({check.split("/")[0] for check in checks}, set(SMOKE))
        self.assertEqual(checks["asn1.der.000001/decode"]["result"],
                         {"outcome": "accept", "tree": {"class": "universal", "constructed": False, "number": "1",
                                                        "contents": "ff"}})
        self.assertEqual(checks["asn1.der.000006/encode"]["result"], {"outcome": "accept", "octets": "3003020105"})
        self.assertEqual(checks["asn1.ber.000001/decode"]["execution"], "unsupported")
        self.assertNotIn("request", checks["asn1.ber.000001/decode"])
        end = records[-1]
        self.assertEqual((end["complete"], end["checks"], end["restarts"], end["exit_status"]),
                         (True, len(resolved.checks), 0, 0))
        self.assertEqual(end["executions"], {"completed": len(resolved.checks) - 1, "unsupported": 1})

    def test_provenance(self):
        resolved = self.resolve({})
        header = self.run_plan(resolved)[0]
        self.assertEqual(header["resolved"], resolved.identity)
        self.assertEqual(header["corpus"], resolved.corpus.to_json())
        self.assertEqual(header["plan"], resolved.plan.to_json())
        self.assertEqual(header["adapter"], resolved.adapter.to_json())
        self.assertEqual(header["descriptor"], DESCRIPTOR)
        self.assertEqual(plan.json_digest(header["descriptor"]), resolved.adapter.descriptor)
        self.assertEqual(header["limits"], resolved.limits.to_json())
        self.assertEqual(header["command"], STUB)
        self.assertEqual(set(header["environment"]), {"system", "release", "machine", "python"})
        self.assertEqual(header["handshake"]["request"], "1")
        self.assertEqual(header["containment"]["tree"], "job-object" if os.name == "nt" else "process-group")

    def test_every_check_is_recorded_in_plan_order(self):
        resolved = self.resolve({})
        checks = self.checks(self.run_plan(resolved))
        self.assertEqual(list(checks), [check.id for check in resolved.checks])
        for check in resolved.checks:
            record = checks[check.id]
            self.assertEqual((record["digest"], record["status"]), (check.digest, check.status))
            if check.applicability == plan.UNSUPPORTED:
                self.assertEqual((record["execution"], record["problem"], record["detail"]),
                                 ("unsupported", check.problem, check.detail))
                self.assertNotIn("request", record)

    def test_requests_carry_no_oracle(self):
        resolved = self.resolve({})
        recorded = self.work / "requests.jsonl"
        records = self.run_plan(resolved, "--record", str(recorded))
        requests = [json.loads(line) for line in recorded.read_bytes().splitlines()]
        executes = [message for message in requests if message["method"] == "execute"]
        sent = {record["request"]: record for record in self.checks(records).values() if "request" in record}
        self.assertEqual({message["id"] for message in executes}, set(sent))
        for message in executes:
            record = sent[message["id"]]
            case = self.release.corpus.sets["conformance"].cases[case_path(record["case"])]
            self.assertEqual(protocol.schema_errors(f"{protocol.REQUEST}#/$defs/executeRequest", message), [])
            self.assertEqual(message["params"], check_contract.oracle_blind_view(case, record["assertion"]))
            self.assertEqual(check_contract.expectation_keys(message), set())
            self.assertNotIn(record["case"], json.dumps(message))
        # Nor do the records of the run: no expectation reaches the observation log either.
        for record in records:
            self.assertEqual(check_contract.expectation_keys({k: v for k, v in record.items() if k != "result"}),
                             set())

    def test_native_diagnostics_are_kept_verbatim(self):
        resolved = self.resolve({"cases": ["asn1.der.000005"]})
        record = self.checks(self.run_plan(resolved))["asn1.der.000005/decode"]
        self.assertEqual(record["result"]["diagnostic"],
                         {"code": "STUB_LENGTH_NON_MINIMAL", "message": "the long length form is not minimal"})


class FailureTest(RunTestCase):
    FAST = {"execute_timeout_ms": 1000, "exit_timeout_ms": 2000}

    def test_timeout_restarts_and_keeps_the_other_checks(self):
        resolved = self.resolve({"cases": ["asn1.der.000001", "asn1.der.000006"]}, limits=self.FAST)
        records = self.run_plan(resolved, "--fail", "hang@2")
        checks = self.checks(records)
        self.assertEqual([record["execution"] for record in checks.values()], ["completed", "timeout", "completed"])
        timeout = checks["asn1.der.000001/round-trip"]
        self.assertNotIn("result", timeout)
        self.assertIn("exit_status", timeout)
        self.assertEqual(checks["asn1.der.000006/encode"]["result"], {"outcome": "accept", "octets": "3003020105"})
        self.assertEqual(self.events(records), ["restart"])
        self.assertEqual((records[-1]["restarts"], records[-1]["complete"]), (1, True))

    def test_crash(self):
        resolved = self.resolve({}, limits=self.FAST)
        sent = [record for record in self.checks(self.run_plan(resolved, "--fail", "crash@1")).values()
                if "request" in record]
        self.assertTrue(all(record["execution"] == "crashed" for record in sent))
        self.assertEqual({(record["problem"], record["exit_status"]) for record in sent}, {("exited", 3)})
        self.assertIn("stub: simulated crash", sent[0]["stderr"])

    def test_malformed_output(self):
        resolved = self.resolve({"cases": ["asn1.der.000001"]}, limits=self.FAST)
        for mode, problem, answer in (("garbage", "json-invalid", {"text": "this is not json"}),
                                      ("noise", "json-invalid", {"text": "starting"}),
                                      ("wrong-id", "id-mismatch", None),
                                      ("huge", "message-too-large", None)):
            with self.subTest(mode):
                records = self.run_plan(resolved, "--fail", f"{mode}@1")
                first = self.checks(records)["asn1.der.000001/decode"]
                self.assertEqual((first["execution"], first["problem"]), ("protocol-error", problem))
                self.assertNotIn("result", first)
                if answer is not None:
                    self.assertEqual(first["answer"], answer)
                elif mode == "wrong-id":
                    self.assertIn('-other"', first["answer"]["text"])
                else:
                    self.assertNotIn("answer", first)
                # The restarted adapter answers the next check.
                second = self.checks(records)["asn1.der.000001/round-trip"]
                self.assertEqual(second["execution"], "protocol-error")
                self.assertEqual(records[-1]["restarts"], 1)

    def test_internal_error_keeps_the_process(self):
        resolved = self.resolve({"cases": ["asn1.der.000001"]})
        records = self.run_plan(resolved, "--fail", "internal@1")
        checks = self.checks(records)
        self.assertEqual((checks["asn1.der.000001/decode"]["execution"], checks["asn1.der.000001/decode"]["problem"]),
                         ("crashed", "internal-error"))
        self.assertEqual(checks["asn1.der.000001/decode"]["error"]["message"], "simulated failure")
        self.assertEqual(checks["asn1.der.000001/round-trip"]["execution"], "completed")
        self.assertEqual(records[-1]["restarts"], 0)

    def test_repetitions_never_replace_the_first_execution(self):
        resolved = self.resolve({"cases": ["asn1.der.000006"]}, limits=self.FAST)
        records = self.run_plan(resolved, "--fail", "crash@1", repeat_failures=2)
        record = self.checks(records)["asn1.der.000006/encode"]
        self.assertEqual(record["execution"], "crashed")
        self.assertEqual([attempt["execution"] for attempt in record["repetitions"]], ["crashed", "crashed"])
        self.assertEqual(len({record["request"]} | {attempt["request"] for attempt in record["repetitions"]}), 3)
        # An internal error leaves the process running, but its repetition still runs in a new one.
        records = self.run_plan(resolved, "--fail", "internal@1", repeat_failures=1)
        record = self.checks(records)["asn1.der.000006/encode"]
        self.assertEqual((record["execution"], record["problem"]), ("crashed", "internal-error"))
        self.assertEqual(record["repetitions"][0]["execution"], "crashed")
        self.assertEqual(records[-1]["restarts"], 1)

    def test_changed_adapter_ends_the_session(self):
        descriptor = copy.deepcopy(DESCRIPTOR)
        descriptor["adapter"]["version"] = "1.0.1"
        resolved = self.resolve({}, descriptor, limits=self.FAST)
        records = self.run_plan(resolved, "--fail", "crash@1", "--vary-identity", "--launch-file",
                                str(self.work / "launches"))
        checks = list(self.checks(records).values())
        sent = [record for record in checks if record["execution"] not in ("unsupported",)]
        self.assertEqual(sent[0]["execution"], "crashed")
        self.assertTrue(all(record["execution"] == run.NOT_RUN for record in sent[1:]))
        self.assertEqual({record["problem"] for record in sent[1:]}, {"session-ended"})
        self.assertIn("adapter-changed", sent[1]["detail"])
        self.assertIn("adapter-changed", self.events(records))
        self.assertFalse(records[-1]["complete"])
        self.assertEqual(records[-1]["checks"], len(resolved.checks))

    def test_infrastructure_failures_are_retried_a_bounded_number_of_times(self):
        resolved = self.resolve({"cases": ["asn1.der.000001"]})
        spawn_failed = protocol.Exchange(None, "hello", "harness-error", "spawn-failed", "no such file", None, "",
                                         None)

        class Session:
            descriptor, restarts, events, running = DESCRIPTOR, 0, [], False
            containment = protocol.Containment()
            calls = 0

            def execute(self, params, requires=()):
                Session.calls += 1
                raise protocol.SessionError("start-failed", "cannot start the adapter", spawn_failed)

            def close(self):
                return None

        stream = io.BytesIO()
        runner = run.Runner(resolved, self.release, Session(), run.Log(stream), start_attempts=3)
        handshake = protocol.Exchange("1", "hello", "completed", None, "", None, "", None, None, 0.0)
        end = runner.run(handshake, ["adapter"])
        records = parse(stream.getvalue())
        self.assertEqual(Session.calls, 3)
        self.assertEqual(self.events(records), ["start-failed"] * 3)
        self.assertEqual([record["execution"] for record in self.checks(records).values()], [run.NOT_RUN] * 2)
        self.assertFalse(end["complete"])
        self.assertNotIn("exit_status", end)

    @unittest.skipIf(sys.platform == "darwin", "macOS does not enforce RLIMIT_AS")
    def test_memory_limit(self):
        resolved = self.resolve({"cases": ["asn1.der.000006"]})
        records = self.run_plan(resolved, "--fail", "hog@1", memory_octets=256 * 1024 * 1024)
        self.assertEqual(records[0]["containment"]["memory_octets"], 256 * 1024 * 1024)
        self.assertEqual(records[0]["containment"]["memory"], "job-object" if os.name == "nt" else "rlimit-as")
        record = self.checks(records)["asn1.der.000006/encode"]
        self.assertEqual((record["execution"], record["problem"]), ("crashed", "exited"))
        self.assertIn("MemoryError", record["stderr"])

    def test_the_process_tree_is_killed(self):
        resolved = self.resolve({"cases": ["asn1.der.000006"]}, limits={"execute_timeout_ms": 3000})
        child_file = self.work / "child"
        records = self.run_plan(resolved, "--fail", "orphan@1", "--child-file", str(child_file))
        self.assertEqual(self.checks(records)["asn1.der.000006/encode"]["execution"], "timeout")
        pid = int(child_file.read_text())
        deadline = time.monotonic() + 10
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        try:
            self.assertFalse(alive(pid), "the adapter's child outlived the adapter")
        finally:
            kill(pid)


class MainTest(RunTestCase):
    def main(self, *arguments):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = run.main([str(argument) for argument in arguments])
        return status, stderr.getvalue()

    def write_resolved(self, resolved):
        path = self.work / "resolved.json"
        path.write_bytes(resolved.dumps())
        return path

    def test_writes_an_immutable_log(self):
        resolved = self.write_resolved(self.resolve(EXAMPLES["smoke.json"]["select"]))
        output = self.work / "observations.jsonl"
        arguments = ("--corpus", self.archive, "--resolved", resolved, "--output", output, "--", *STUB)
        status, stderr = self.main(*arguments)
        self.assertEqual(status, run.EXIT_COMPLETE, stderr)
        self.assertIn("run complete", stderr)
        records = parse(output.read_bytes())
        self.assertEqual((records[0]["record"], records[-1]["record"]), ("run", "end"))
        before = output.read_bytes()
        status, stderr = self.main(*arguments)
        self.assertEqual(status, run.EXIT_REFUSED)
        self.assertIn("output-exists", stderr)
        self.assertEqual(output.read_bytes(), before)

    def test_refuses_another_adapter_without_writing(self):
        descriptor = copy.deepcopy(DESCRIPTOR)
        descriptor["implementation"]["version"] = "2.0.0"
        resolved = self.write_resolved(self.resolve({}, descriptor))
        output = self.work / "observations.jsonl"
        status, stderr = self.main("--corpus", self.archive, "--resolved", resolved, "--output", output, "--", *STUB)
        self.assertEqual(status, run.EXIT_REFUSED)
        self.assertIn("adapter-mismatch", stderr)
        self.assertFalse(output.exists())

    def test_refuses_an_adapter_that_does_not_start(self):
        resolved = self.write_resolved(self.resolve({}))
        output = self.work / "observations.jsonl"
        status, stderr = self.main("--corpus", self.archive, "--resolved", resolved, "--output", output, "--",
                                   *STUB, "--protocols", "2")
        self.assertEqual(status, run.EXIT_REFUSED)
        self.assertIn("adapter-start-failed", stderr)
        self.assertFalse(output.exists())

    def test_incomplete_run(self):
        descriptor = copy.deepcopy(DESCRIPTOR)
        descriptor["adapter"]["version"] = "1.0.1"
        resolved = self.write_resolved(self.resolve({}, descriptor, limits=FailureTest.FAST))
        output = self.work / "observations.jsonl"
        status, stderr = self.main("--corpus", self.archive, "--resolved", resolved, "--output", output, "--", *STUB,
                                   "--fail", "crash@1", "--vary-identity", "--launch-file", self.work / "launches")
        self.assertEqual(status, run.EXIT_INCOMPLETE, stderr)
        self.assertIn("run incomplete", stderr)


@unittest.skipUnless(CORE_ADAPTER, "set OPENTLV_CORPUS_CORE_ADAPTER to the OpenTLV Core adapter command")
class CoreAdapterTest(RunTestCase):
    """The smoke plan against the OpenTLV Core adapter, which OpenTLV TestLab builds and maintains."""

    def test_smoke_plan(self):
        command = shlex.split(CORE_ADAPTER, posix=os.name != "nt")
        session = protocol.Session(command)
        try:
            session.start()
        finally:
            session.close()
        resolved = self.resolve(EXAMPLES["smoke.json"]["select"], session.descriptor)
        stream = io.BytesIO()
        end = run.run(resolved, self.release, command, stream)
        records = parse(stream.getvalue())
        for record in records:
            self.assertEqual(check_contract.schema_errors("observations", record), [], record)
        checks = self.checks(records)
        self.assertTrue(end["complete"])
        self.assertEqual([check_id for check_id, record in checks.items() if record["execution"] in FAILURES], [])
        self.assertTrue(all(checks[check.id]["execution"] == "completed" for check in resolved.checks
                            if check.applicability == plan.APPLICABLE))


if __name__ == "__main__":
    unittest.main()
