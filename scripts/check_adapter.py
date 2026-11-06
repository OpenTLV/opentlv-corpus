# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Check that an adapter follows the adapter process protocol.

The check starts the adapter command and runs one fixed sequence: a request
before the handshake, the handshake, malformed and out-of-order requests,
every example assertion the adapter's descriptor covers, a repeated request
and the shutdown. It checks the protocol, not the implementation: it never
compares a result with an expectation, and a crash or timeout of the
implementation is counted, not reported as a problem.

Each problem is one line, "<step>: <code>: <detail>", and the check exits with
status 1 when it reports any.

Run it with the adapter command after "--".
"""

import argparse
from collections import Counter
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import check_contract, protocol  # noqa: E402

# A request every adapter can parse, whether or not it supports it.
PROBE = {"standard": {"ref": "itu-t-x690-2021-02", "rules": "der"},
         "request": {"operation": "decode", "strictness": "strict", "view": "tree"},
         "input": {"octets": "020105"}}
# Statuses that show the implementation failed rather than the protocol.
IMPLEMENTATION_FAILURES = ("crashed", "timeout")


class Check:
    def __init__(self, command, limits):
        self.session = protocol.Session(command, limits)
        self.problems = []
        self.counts = Counter()

    def report(self, step, code, detail=""):
        self.problems.append(f"{step}: {code}: {detail}" if detail else f"{step}: {code}")

    def expect(self, step, exchange, status):
        if exchange.status != status:
            detail = f"{exchange.problem}: {exchange.detail}" if exchange.problem else exchange.detail
            self.report(step, exchange.status, f"expected {status}; {detail}" if detail else f"expected {status}")

    def ensure_started(self):
        if not self.session.running:
            self.session.start()

    def send_raw(self, step, text, request_id):
        self.ensure_started()
        exchange = self.session.send_line(text.encode("utf-8") + b"\n", "execute", request_id,
                                          self.session.limits.execute_timeout)
        self.expect(step, exchange, "bad-request")

    def run(self):
        timeout = self.session.limits.execute_timeout
        self.session.spawn()
        exchange = self.session.send(protocol.request(self.session.next_id(), "execute", PROBE), timeout, PROBE)
        self.expect("order", exchange, "bad-request")
        if not self.session.running:
            self.session.spawn()
        descriptor = self.session.hello().response["result"]

        self.send_raw("malformed", '{"id":"1","method":"execute"', None)
        self.send_raw("malformed", '["execute"]', None)
        request_id = self.session.next_id()
        self.send_raw("malformed", f'{{"id":"{request_id}","method":"no-such-method","params":{{}}}}', request_id)
        request_id = self.session.next_id()
        self.send_raw("malformed", f'{{"id":"{request_id}","method":"execute"}}', request_id)
        self.ensure_started()
        hello = protocol.request(self.session.next_id(), "hello",
                                 protocol.hello_params(self.session.limits.max_message_octets))
        self.expect("order", self.session.send(hello, self.session.limits.hello_timeout), "bad-request")

        first = None
        valid, _ = check_contract.load_examples()
        for name, case in valid.items():
            for assertion in case["normative"]["assertions"]:
                step = f"examples/{name}/{assertion['key']}"
                params = protocol.execute_params(case, assertion["key"])
                exchange = self.session.execute(params, assertion.get("requires", ()))
                self.counts[exchange.status] += 1
                if exchange.status in ("bad-request", "protocol-error", "harness-error"):
                    self.expect(step, exchange, "completed")
                if exchange.status == "completed" and first is None:
                    first = params, exchange.response["result"]
        if first is not None:
            again = self.session.execute(first[0])
            if again.status != "completed" or again.response["result"] != first[1]:
                self.report("idempotency", "not-idempotent", f"the repeated request ended with {again.status}")

        self.session.close()
        for code, detail in self.session.events:
            if code != "restart":
                self.report("session", code, detail)
        return descriptor


def check(command, limits=protocol.Limits()):
    """Run the check and return the problems, the descriptor and the counts of the example statuses."""
    run = Check(command, limits)
    descriptor = None
    try:
        descriptor = run.run()
    except protocol.SessionError as error:
        run.report("session", error.code, error.detail)
        run.session.close()
    return run.problems, descriptor, run.counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     usage="%(prog)s [--timeout SECONDS] -- COMMAND [ARGUMENT ...]")
    parser.add_argument("--timeout", type=float, default=protocol.Limits.execute_timeout,
                        help="seconds the adapter may take to answer one request")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="the adapter command")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("the adapter command is missing")
    limits = protocol.Limits(hello_timeout=args.timeout, execute_timeout=args.timeout)
    problems, descriptor, counts = check(command, limits)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"adapter nonconforming: {len(problems)} problems", file=sys.stderr)
        return 1
    statuses = ", ".join(f"{counts[status]} {status}" for status in protocol.STATUSES if counts[status])
    adapter, implementation = descriptor["adapter"], descriptor["implementation"]
    print(f"adapter ok: {adapter['name']} {adapter['version']} for {implementation['name']} "
          f"{implementation['version']}; examples: {statuses}")
    if any(counts[status] for status in IMPLEMENTATION_FAILURES):
        print("the implementation crashed or timed out on some examples", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
