# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Run a resolved execution plan against one adapter and record the observations.

The runner checks the resolved plan against the release and the adapter it is
given, then sends the oracle-blind request of every applicable check to the
adapter process, one at a time, and appends what happened to an observation
log: the adapter's raw result or error, the classification of the exchange,
its native diagnostics and the session events, after a run record with the
provenance of the run. A check the resolved plan marks unsupported is
recorded without being sent. A crash, timeout or protocol error ends only the
adapter process, which is restarted for the next check; a request the
harness never sent is recorded as not run, never with a result. The log
holds no expectation and no verdict: the verdict engine evaluates it later.

docs/runner.md specifies the runner and the observation log.
"""

import argparse
from collections import Counter
import dataclasses
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import plan, protocol, release  # noqa: E402

OBSERVATIONS = "urn:opentlv:corpus:schema:observations:1"
NOT_RUN = "not-run"
# Failures of the implementation or its adapter that a run may repeat in a new process to learn whether they
# reproduce; harness errors are retried when the adapter is started instead.
REPEATABLE = ("crashed", "timeout", "protocol-error")
EXIT_COMPLETE, EXIT_REFUSED, EXIT_INCOMPLETE = 0, 1, 2


class RunError(Exception):
    """The run cannot start; problems are (code, detail) pairs."""

    def __init__(self, problems):
        super().__init__("; ".join(f"{code}: {detail}" for code, detail in problems))
        self.problems = problems


def now():
    """Return the current UTC time as an RFC 3339 timestamp with a Z suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def environment():
    return {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "python": platform.python_version()}


def _milliseconds(seconds):
    return round(seconds * 1000)


def _answer(line):
    try:
        return {"text": line.decode("utf-8")}
    except UnicodeDecodeError:
        return {"hex": line.hex()}


def observation(exchange):
    """Return the members of a record that describe one exchange, leaving out the empty ones."""
    record = {"execution": exchange.status}
    if exchange.problem is not None:
        record["problem"] = exchange.problem
    if exchange.detail:
        record["detail"] = exchange.detail
    if exchange.id is not None:
        record["request"] = exchange.id
    if exchange.response is not None:
        if "result" in exchange.response:
            record["result"] = exchange.response["result"]
        else:
            record["error"] = exchange.response["error"]
    if exchange.answer is not None:
        record["answer"] = _answer(exchange.answer)
    if exchange.stderr:
        record["stderr"] = exchange.stderr
    if exchange.exit_status is not None:
        record["exit_status"] = exchange.exit_status
    if exchange.duration is not None:
        record["duration_ms"] = _milliseconds(exchange.duration)
    return record


class Log:
    """An observation log: one JSON object per line, flushed line by line, never rewritten."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, record):
        self.stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                          .encode("utf-8") + b"\n")
        self.stream.flush()


class Runner:
    """One run of a resolved plan against one adapter session."""

    def __init__(self, resolved, release_, session, log, repeat_failures=0, start_attempts=3):
        self.resolved = resolved
        self.release = release_
        self.session = session
        self.log = log
        self.repeat_failures = repeat_failures
        self.start_attempts = start_attempts
        self.cases, _, _ = plan._corpus(release_)
        self.ended = None
        self.executions = Counter()
        self._events = 0

    def _flush_events(self, check=None):
        """Write the session events recorded since the last call."""
        for code, detail in self.session.events[self._events:]:
            record = {"record": "session", "event": code, "detail": detail}
            if check is not None:
                record["check"] = check
            self.log.write(record)
        self._events = len(self.session.events)

    def _failed_start(self, error, check):
        """Record a failed start of the adapter, with what it wrote to standard error."""
        self._flush_events(check)
        record = {"record": "session", "event": error.code, "detail": error.detail, "check": check}
        if error.exchange is not None and error.exchange.stderr:
            record["stderr"] = error.exchange.stderr
        self.log.write(record)

    def _execute(self, params, requires, check):
        """Execute one request; return its exchange, or None once the session has ended.

        Only a harness error, an adapter that cannot be started, is retried,
        up to the start attempts; any other failed start ends the session.
        """
        attempts = 0
        while True:
            try:
                return self.session.execute(params, requires)
            except protocol.SessionError as error:
                attempts += 1
                self._failed_start(error, check)
                if not (error.code == "start-failed" and error.exchange is not None
                        and error.exchange.status == "harness-error" and attempts < self.start_attempts):
                    self.ended = f"the session ended: {error.code}: {error.detail}"
                    return None

    def _repeat(self, params, requires, check):
        """Repeat a failed request in new processes and return the attempts."""
        attempts = []
        for _ in range(self.repeat_failures):
            if self.session.running:
                # A repetition runs in a new process, also after an internal error.
                try:
                    self.session.restart(f"repetition of {check}")
                except protocol.SessionError as error:
                    self._failed_start(error, check)
                    self.ended = f"the session ended: {error.code}: {error.detail}"
                    break
            exchange = self._execute(params, requires, check)
            if exchange is None:
                break
            attempts.append(observation(exchange))
        return attempts

    def run_check(self, check):
        record = {"record": "check", "case": check.case, "assertion": check.assertion, "digest": check.digest,
                  "status": check.status}
        if check.applicability == plan.UNSUPPORTED:
            record.update(execution="unsupported", problem=check.problem, detail=check.detail)
        elif self.ended is not None:
            record.update(execution=NOT_RUN, problem="session-ended", detail=self.ended)
        else:
            case = self.cases[check.case]
            assertion = next(item for item in case["normative"]["assertions"] if item["key"] == check.assertion)
            params = protocol.execute_params(case, check.assertion)
            exchange = self._execute(params, assertion.get("requires", ()), check.id)
            if exchange is None:
                record.update(execution=NOT_RUN, problem="session-ended", detail=self.ended)
            else:
                record.update(observation(exchange))
                if exchange.status in REPEATABLE and self.repeat_failures:
                    repetitions = self._repeat(params, assertion.get("requires", ()), check.id)
                    if repetitions:
                        record["repetitions"] = repetitions
        self._flush_events(check.id)
        self.executions[record["execution"]] += 1
        self.log.write(record)

    def run(self, handshake, command):
        resolved = self.resolved
        header = {"schema": OBSERVATIONS, "record": "run", "resolved": resolved.identity,
                  "plan": resolved.plan.to_json(), "corpus": resolved.corpus.to_json(),
                  "tools": dict(release.TOOLS), "adapter": resolved.adapter.to_json(),
                  "descriptor": self.session.descriptor, "command": list(command),
                  "limits": resolved.limits.to_json(), "containment": self.session.containment.to_json(),
                  "retries": {"repeat_failures": self.repeat_failures, "start_attempts": self.start_attempts},
                  "environment": environment(), "started": now()}
        header["handshake"] = {"request": handshake.id, "duration_ms": _milliseconds(handshake.duration or 0)}
        if handshake.stderr:
            header["handshake"]["stderr"] = handshake.stderr
        self.log.write(header)
        self._events = len(self.session.events)
        for check in resolved.checks:
            self.run_check(check)
        exit_status = self.session.close()
        self._flush_events()
        end = {"record": "end", "finished": now(), "complete": self.ended is None, "checks": len(resolved.checks),
               "executions": dict(sorted(self.executions.items())), "restarts": self.session.restarts}
        if exit_status is not None:
            end["exit_status"] = exit_status
        self.log.write(end)
        return end


def prepare(resolved, release_, command, memory_octets=None, cwd=None, env=None):
    """Start the adapter and check the resolved plan against the release and the adapter.

    Return the session and the exchange of its handshake, or raise RunError
    with the adapter stopped.
    """
    limits = dataclasses.replace(resolved.limits.harness(), memory_octets=memory_octets)
    session = protocol.Session(command, limits, cwd, env)
    try:
        handshake = session.start()
    except protocol.SessionError as error:
        session.close()
        raise RunError([("adapter-" + error.code, error.detail)]) from error
    problems = plan.check_resolved(resolved, release_, session.descriptor)
    if problems:
        session.close()
        raise RunError(problems)
    return session, handshake


def run(resolved, release_, command, stream, memory_octets=None, repeat_failures=0, start_attempts=3, cwd=None,
        env=None):
    """Run a resolved plan with an adapter command and write the observation log to a binary stream.

    Return the end record. Raise RunError, without writing anything, when the
    plan cannot run against this release and adapter.
    """
    session, handshake = prepare(resolved, release_, command, memory_octets, cwd, env)
    runner = Runner(resolved, release_, session, Log(stream), repeat_failures, start_attempts)
    try:
        return runner.run(handshake, command)
    finally:
        session.close()


def describe(end):
    counts = ", ".join(f"{count} {status}" for status, count in end["executions"].items())
    state = "complete" if end["complete"] else "incomplete"
    return f"run {state}: {end['checks']} checks, {counts}; {end['restarts']} restarts"


def _octets(text):
    units = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}
    try:
        value = int(text[:-1]) * units[text[-1].lower()] if text[-1:].lower() in units else int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of octets such as 268435456 or 256M") from None
    if value < 1:
        raise argparse.ArgumentTypeError("the memory limit is at least one octet")
    return value


def _count(low, high):
    def parse(text):
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"{value} is not between {low} and {high}")
        return value
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        usage="%(prog)s --corpus ARCHIVE [--sums SHA256SUMS] --resolved FILE --output FILE [options] "
              "-- COMMAND [ARGUMENT ...]")
    parser.add_argument("--corpus", required=True, help="the data release archive")
    parser.add_argument("--sums", help="the SHA256SUMS published with the archive")
    parser.add_argument("--resolved", required=True, help="the resolved execution plan")
    parser.add_argument("--output", required=True, help="the observation log to create; never overwritten")
    parser.add_argument("--memory-limit", type=_octets, metavar="OCTETS",
                        help="memory each adapter process may commit, such as 256M; see docs/runner.md for the "
                             "platforms that enforce it")
    parser.add_argument("--repeat-failures", type=_count(0, 100), default=0, metavar="N",
                        help="repeat a crashed, timed-out or protocol-breaking request N times in new processes")
    parser.add_argument("--start-attempts", type=_count(1, 100), default=3, metavar="N",
                        help="attempts to start the adapter after an infrastructure failure (default 3)")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the adapter command, after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the adapter command after --")
    output = Path(args.output)
    try:
        if output.exists():
            raise RunError([("output-exists", f"{output} exists; an observation log is never overwritten")])
        opened, problems = release.open_release(args.corpus, args.sums)
        if problems:
            raise RunError([("release-invalid", str(problem)) for problem in problems])
        resolved = plan.read(args.resolved, plan.load_resolved)
        session, handshake = prepare(resolved, opened, command, args.memory_limit)
    except plan.PlanError as error:
        problems = error.problems
    except RunError as error:
        problems = error.problems
    else:
        problems = []
    for code, detail in problems:
        print(f"{code}: {detail}", file=sys.stderr)
    if problems:
        print(f"plan not run: {len(problems)} problems", file=sys.stderr)
        return EXIT_REFUSED
    if session.containment.memory == "unenforced":
        print(f"warning: {platform.system()} does not enforce the memory limit", file=sys.stderr)
    try:
        with open(output, "xb") as stream:
            end = Runner(resolved, opened, session, Log(stream), args.repeat_failures, args.start_attempts) \
                .run(handshake, command)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        session.close()
    print(describe(end), file=sys.stderr)
    print(f"observations {output}: sha256 {hashlib.sha256(output.read_bytes()).hexdigest()}", file=sys.stderr)
    return EXIT_COMPLETE if end["complete"] else EXIT_INCOMPLETE


if __name__ == "__main__":
    sys.exit(main())
