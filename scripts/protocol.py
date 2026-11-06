# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Harness side of the adapter process protocol, version 1.

An adapter is a separate process that speaks JSON Lines on its standard
streams. This module frames and validates the messages, builds the
oracle-blind execute parameters of an assertion, decides which requests the
adapter's capability descriptor covers, and drives one adapter process at a
time: it sends one request, waits for its answer within a time limit and
classifies the exchange. After a crash, a timeout or a protocol error it kills
the process and starts a new one before the next request. It never invents a
result for a request that has no valid answer.

docs/adapter-protocol.md specifies the protocol.
"""

from collections import namedtuple
from dataclasses import dataclass
import functools
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading


if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import canonical, check_contract  # noqa: E402

PROTOCOL_DIR = check_contract.ROOT / "protocol"
VERSION = 1
URN = "urn:opentlv:corpus:protocol:{kind}:{version}"
SCHEMA_KINDS = ("request", "response", "capabilities")
REQUEST = URN.format(kind="request", version=VERSION)
RESPONSE = URN.format(kind="response", version=VERSION)
CAPABILITIES = URN.format(kind="capabilities", version=VERSION)

PARAMS = ("standard", "type", "input", "request")
STATUSES = ("completed", "unsupported", "not-comparable", "bad-request", "protocol-error", "crashed", "timeout",
            "harness-error")
# The error codes each method may answer, and the status each one gives the exchange.
ERROR_CODES = {"hello": ("version-unsupported", "bad-request", "internal-error"),
               "execute": ("bad-request", "unsupported", "not-comparable", "internal-error")}
ERROR_STATUS = {"version-unsupported": "unsupported", "bad-request": "bad-request", "unsupported": "unsupported",
                "not-comparable": "not-comparable", "internal-error": "crashed"}


@dataclass(frozen=True)
class Limits:
    """Time and size bounds the harness enforces; the runner records the values it used."""

    hello_timeout: float = 10.0
    execute_timeout: float = 10.0
    exit_timeout: float = 5.0
    max_message_octets: int = 4 * 1024 * 1024
    stderr_octets: int = 64 * 1024


Exchange = namedtuple("Exchange", "id method status problem detail response stderr exit_status")
Exchange.__doc__ = """One request and its classification.

id is None when the harness decided without sending anything. problem is a
stable code that refines the status, detail explains it, and response is the
parsed answer when there is a valid one. stderr is what the adapter wrote to
its standard error during the exchange, and exit_status the status of a
process that ended or was killed.
"""


class SessionError(Exception):
    """The adapter cannot be used: it did not start, or a restarted adapter is a different one."""

    def __init__(self, code, detail, exchange=None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.exchange = exchange


@functools.lru_cache(maxsize=None)
def _schemas():
    return {kind: canonical.load_json(PROTOCOL_DIR / f"{kind}-{VERSION}.schema.json") for kind in SCHEMA_KINDS}


@functools.lru_cache(maxsize=None)
def _validator(ref):
    # The protocol schemas are embedded as resources, so references between
    # them resolve by URN without fetching anything.
    return check_contract.Validator({"$schema": "https://json-schema.org/draft/2020-12/schema",
                                     "$defs": _schemas(), "$ref": ref})


def schema_errors(ref, message):
    """Return the sorted "pointer (keyword)" schema errors of a message against a schema or definition URI."""
    return sorted({f"/{'/'.join(str(part) for part in error.absolute_path)} ({error.validator})"
                   for error in _validator(ref).iter_errors(message)})


def response_ref(method, message):
    """Return the definition an answer to a method must match."""
    if isinstance(message, dict) and "error" in message:
        return f"{RESPONSE}#/$defs/errorResponse"
    return f"{RESPONSE}#/$defs/{method}Response"


def encode(message):
    """Return a message as one line: compact ASCII JSON and a line feed."""
    return json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("ascii") + b"\n"


def request(request_id, method, params):
    return {"id": request_id, "method": method, "params": params}


def hello_params(max_message_octets):
    return {"protocols": [VERSION], "max_message_octets": max_message_octets}


def execute_params(case, key):
    """Return the execute parameters of one assertion: its oracle-blind view and nothing else."""
    return check_contract.oracle_blind_view(case, key)


def depth(node):
    """Return the nesting depth of a TLV tree node or a typed value; a primitive has depth 1."""
    children = node.get("children") or [field["value"] for field in node.get("fields", ())] or node.get("items")
    if node.get("kind") == "choice":
        children = [node["value"]]
    return 1 + max((depth(child) for child in children or ()), default=0)


def applicability(descriptor, params, requires=()):
    """Return the (problem, detail) that keeps a descriptor from covering a request, or None."""
    standard, request_ = params["standard"], params["request"]
    if not any(entry["standard"] == standard["ref"] and standard["rules"] in entry["rules"]
               and request_["operation"] in entry["operations"] and request_["strictness"] in entry["strictness"]
               and request_["view"] in entry["views"] for entry in descriptor["supports"]):
        return ("undeclared", f"{standard['ref']} {standard['rules']} {request_['operation']} "
                              f"{request_['strictness']} {request_['view']}")
    missing = sorted(set(requires) - set(descriptor["provides"]))
    if missing:
        return "capability", ", ".join(missing)
    limits = descriptor["limits"]
    kind, subject = next(iter(params["input"].items()))
    if kind == "octets" and len(subject) // 2 > limits.get("max_input_octets", len(subject)):
        return "input-too-large", f"{len(subject) // 2} octets, at most {limits['max_input_octets']}"
    if kind != "octets" and depth(subject) > limits.get("max_depth", depth(subject)):
        return "too-deep", f"depth {depth(subject)}, at most {limits['max_depth']}"
    return None


def round_trip_steps(params):
    """Return the output kinds of a complete round trip: decode, encode, decode, or encode, decode."""
    view = params["request"]["view"]
    return [view, "octets", view] if "octets" in params["input"] else ["octets", view]


def result_problems(params, result):
    """Return why a schema-valid execute result does not fit its request, as detail strings."""
    operation, view = params["request"]["operation"], params["request"]["view"]
    if operation == "round-trip":
        expected = round_trip_steps(params)
        steps = [next(iter(step)) for step in result.get("steps", ())]
        if result["outcome"] == "accept" and steps != expected:
            return [f"a round trip of this input yields steps {', '.join(expected)}, not {', '.join(steps) or 'none'}"]
        if result["outcome"] == "reject" and (len(steps) >= len(expected) or steps != expected[:len(steps)]):
            return [f"a rejected round trip lists the steps before the failing one of {', '.join(expected)}"]
        return []
    if "steps" in result:
        return ["only a round trip has steps"]
    output = view if operation == "decode" else "octets"
    if result["outcome"] == "accept" and output not in result:
        return [f"an accepted {operation} in the {view} view yields {output}"]
    return []


def classify(line, method, request_id, params=None):
    """Classify one answer line, without its line feed, to a request.

    request_id is None for a line the adapter cannot have read an ID from.
    Return (status, problem, detail, message); message is the parsed answer
    when it is valid.
    """
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as error:
        return "protocol-error", "encoding-invalid", str(error), None
    if not text.strip(" \t\r"):
        return "protocol-error", "json-invalid", "empty line", None
    try:
        message = canonical.loads(text)
    except ValueError as error:
        return "protocol-error", "json-invalid", str(error), None
    errors = schema_errors(response_ref(method, message), message)
    if errors:
        return "protocol-error", "schema", ", ".join(errors), None
    if message["id"] is not None and message["id"] != request_id:
        return "protocol-error", "id-mismatch", f"{message['id']!r} answers {request_id!r}", None
    if "error" in message:
        code = message["error"]["code"]
        if code not in ERROR_CODES[method]:
            return "protocol-error", "answer-mismatch", f"{method} cannot answer {code}", None
        if message["id"] is None and request_id is not None:
            # Every request the harness sends is valid JSON with a readable ID.
            return "protocol-error", "id-mismatch", f"null answers {request_id!r}", None
        return ERROR_STATUS[code], code, message["error"]["message"], message
    if method == "execute":
        problems = result_problems(params, message["result"])
        if problems:
            return "protocol-error", "answer-mismatch", "; ".join(problems), None
    return "completed", None, "", message


class Channel:
    """An adapter process whose standard streams background threads drain.

    The harness never blocks on a pipe: requests are written by a thread, and
    answer lines, the end of the output and write failures arrive as events.
    """

    def __init__(self, command, max_line, stderr_octets, cwd=None, env=None):
        options = {"start_new_session": True} if os.name == "posix" else {}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, cwd=cwd, env=env, **options)
        self.max_line = max_line
        self.stderr_octets = stderr_octets
        self.events = queue.Queue()
        self._stderr = bytearray()
        self._stderr_truncated = False
        self._lock = threading.Lock()
        self._threads = [threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for thread in self._threads:
            thread.start()

    def _read_stdout(self):
        stream = self.process.stdout
        while True:
            line = stream.readline(self.max_line + 1)
            if line.endswith(b"\n"):
                self.events.put(("line", line[:-1]))
            elif len(line) > self.max_line:
                self.events.put(("too-large", f"longer than {self.max_line} octets"))
                return
            else:
                # The output ended, possibly in the middle of a line.
                self.events.put(("eof", line))
                return

    def _read_stderr(self):
        while True:
            chunk = self.process.stderr.read1(4096)
            if not chunk:
                return
            with self._lock:
                room = self.stderr_octets - len(self._stderr)
                self._stderr += chunk[:max(room, 0)]
                self._stderr_truncated |= len(chunk) > room

    def _write(self, data):
        try:
            self.process.stdin.write(data)
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            self.events.put(("write-failed", str(error)))

    def write(self, data):
        threading.Thread(target=self._write, args=(data,), daemon=True).start()

    def next_event(self, timeout):
        try:
            return self.events.get(timeout=max(timeout, 0))
        except queue.Empty:
            return "timeout", None

    def pending(self):
        return not self.events.empty()

    def take_stderr(self):
        """Return the standard error text written since the last call."""
        with self._lock:
            data, truncated = bytes(self._stderr), self._stderr_truncated
            self._stderr.clear()
            self._stderr_truncated = False
        text = data.decode("utf-8", errors="replace")
        return text + "\n[standard error truncated]" if truncated else text

    def close_input(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass

    def wait(self, timeout):
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self):
        """Kill the adapter, with its process group where the platform has one, and return its exit status."""
        # The group outlives an adapter that exited and left children behind.
        try:
            if os.name == "posix":
                os.killpg(self.process.pid, signal.SIGKILL)
            elif self.process.poll() is None:
                self.process.kill()
        except (ProcessLookupError, PermissionError):
            pass
        status = self.process.wait()
        self.close_input()
        for thread in self._threads:
            thread.join(timeout=1.0)
        for stream in (self.process.stdout, self.process.stderr):
            stream.close()
        return status


class Session:
    """One adapter, restarted after failures, that answers one request at a time."""

    def __init__(self, command, limits=Limits(), cwd=None, env=None):
        self.command = list(command)
        self.limits = limits
        self.cwd = cwd
        self.env = env
        self.descriptor = None
        self.restarts = 0
        # What happened to the session outside any request: (code, detail).
        self.events = []
        self._channel = None
        self._serial = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def next_id(self):
        """Return a new request ID; IDs are never reused within a session, across restarts."""
        self._serial += 1
        return str(self._serial)

    @property
    def running(self):
        """Whether the adapter process is running."""
        return self._channel is not None and self._channel.process.poll() is None

    def spawn(self):
        """Start the adapter process without the handshake, ending a previous one."""
        self._discard()
        try:
            self._channel = Channel(self.command, self.limits.max_message_octets, self.limits.stderr_octets,
                                    self.cwd, self.env)
        except OSError as error:
            exchange = Exchange(None, "hello", "harness-error", "spawn-failed", str(error), None, "", None)
            raise SessionError("start-failed", f"cannot start the adapter: {error}", exchange) from error

    def hello(self):
        """Run the handshake and return its exchange; raise SessionError unless it completes."""
        exchange = self.send(request(self.next_id(), "hello", hello_params(self.limits.max_message_octets)),
                             self.limits.hello_timeout)
        if exchange.status != "completed":
            self._discard()
            raise SessionError("start-failed", f"hello ended with {exchange.status}", exchange)
        descriptor = exchange.response["result"]
        if self.descriptor is not None and descriptor != self.descriptor:
            self._discard()
            raise SessionError("adapter-changed", "the restarted adapter describes itself differently", exchange)
        self.descriptor = descriptor
        return exchange

    def start(self):
        self.spawn()
        return self.hello()

    def _restart(self, cause):
        self.events.append(("restart", cause))
        self._discard()
        self.restarts += 1
        self.start()

    def _discard(self):
        if self._channel is not None:
            self._channel.kill()
            self._channel = None

    def execute(self, params, requires=()):
        """Execute one request, or decide without sending it that the adapter does not support it."""
        if self.descriptor is None:
            raise SessionError("not-started", "execute before a completed hello")
        decision = applicability(self.descriptor, params, requires)
        if decision is not None:
            return Exchange(None, "execute", "unsupported", *decision, None, "", None)
        if self._channel is None:
            self._restart("the previous exchange failed")
        elif self._channel.pending():
            event, data = self._channel.next_event(0)
            if event == "line":
                self.events.append(("unsolicited-output", repr(data[:80])))
            else:
                self.events.append(("exited", "the adapter ended between requests"))
            self._restart(f"{self.events[-1][0]} between requests")
        message = request(self.next_id(), "execute", params)
        size = len(encode(message)) - 1
        limit = self.descriptor["limits"]["max_message_octets"]
        if size > limit:
            return Exchange(None, "execute", "unsupported", "message-too-large",
                            f"request of {size} octets, at most {limit}", None, "", None)
        return self.send(message, self.limits.execute_timeout, params)

    def send(self, message, timeout, params=None):
        """Send one request message and classify its answer."""
        line = encode(message)
        return self.send_line(line, message["method"], message["id"], timeout, params)

    def send_line(self, line, method, request_id, timeout, params=None):
        """Send one raw line, which ends with a line feed, and classify the answer as one to request_id."""
        channel = self._channel
        channel.write(line)
        event, data = channel.next_event(timeout)
        response, exit_status = None, None
        if event == "line":
            status, problem, detail, response = classify(data, method, request_id, params)
        elif event == "too-large":
            status, problem, detail = "protocol-error", "message-too-large", data
        elif event == "timeout":
            status, problem, detail = "timeout", None, f"no answer within {timeout:g} s"
        elif event == "write-failed":
            status, problem, detail = "crashed", "write-failed", data
        else:
            status, problem, detail = "crashed", "exited", "the adapter closed its output before answering"
            exit_status = channel.wait(self.limits.exit_timeout)
        if status in ("protocol-error", "timeout", "harness-error") or (status == "crashed"
                                                                        and problem != "internal-error"):
            exit_status = channel.kill()
            self._channel = None
        return Exchange(request_id, method, status, problem, detail, response, channel.take_stderr(), exit_status)

    def close(self):
        """Close the adapter's input and return its exit status, killing it after the grace period."""
        channel, self._channel = self._channel, None
        if channel is None:
            return None
        channel.close_input()
        status = channel.wait(self.limits.exit_timeout)
        if status is None:
            self.events.append(("exit-timeout", f"still running {self.limits.exit_timeout:g} s after its input closed"))
            status = channel.kill()
        else:
            channel.kill()
            if status != 0:
                self.events.append(("exit-status", str(status)))
        while channel.pending():
            event, data = channel.next_event(0)
            if event == "line":
                self.events.append(("unsolicited-output", repr(data[:80])))
        return status
