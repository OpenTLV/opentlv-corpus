# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""A minimal adapter for the adapter process protocol, version 1.

It shows what any adapter does, in any language: read one JSON request per
line from standard input, write one JSON answer per line to standard output,
log only to standard error and exit when standard input ends. It uses nothing
but the Python standard library and none of the corpus tooling.

Its implementation is a small DER decoder and encoder of TLV trees with
definite lengths. It is a protocol fixture, not a conforming implementation.
The --fail option makes it misbehave on purpose, for the harness tests:
MODE@hello acts on the hello request, MODE@N on the Nth execute request of
the process.
"""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time

PROTOCOLS = (1,)
STANDARD = "itu-t-x690-2021-02"
MAX_DEPTH = 32
CLASSES = ("universal", "application", "context", "private")
REQUEST_ID = re.compile(r"[0-9A-Za-z._-]{1,64}")
MODES = ("crash", "hang", "garbage", "wrong-id", "huge", "noise", "internal", "not-comparable", "quit", "unsolicited")


class Reject(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class Unsupported(Exception):
    pass


class BadRequest(Exception):
    pass


def decode_node(data, offset, end, depth):
    """Decode one TLV between offset and end and return the node and the offset after it."""
    if depth > MAX_DEPTH:
        raise Unsupported(f"nesting deeper than {MAX_DEPTH} levels")
    short = "input.truncated" if end == len(data) else "length.exceeds-container"

    def take():
        nonlocal offset
        if offset >= end:
            raise Reject(short, f"the encoding ends at octet {offset}")
        offset += 1
        return data[offset - 1]

    first = take()
    number = first & 0x1F
    if number == 0x1F:
        number, octet = 0, take()
        if octet == 0x80:
            raise Reject("identifier.non-minimal", "the tag number starts with a zero group")
        while True:
            number = number << 7 | (octet & 0x7F)
            if not octet & 0x80:
                break
            octet = take()
        if number < 31:
            raise Reject("identifier.high-form-low-number", f"tag number {number} in the high-tag-number form")
    initial = take()
    if initial == 0x80:
        raise Reject("length.form-forbidden", "DER forbids the indefinite length form")
    if initial == 0xFF:
        raise Reject("length.reserved", "the initial length octet FF is reserved")
    length = initial
    if initial & 0x80:
        octets = bytes(take() for _ in range(initial & 0x7F))
        length = int.from_bytes(octets, "big")
        if octets[0] == 0 or length < 0x80:
            raise Reject("length.non-minimal", "the long length form is not minimal")
    if offset + length > end:
        raise Reject(short, f"length {length} runs past octet {end}")
    node = {"class": CLASSES[first >> 6], "constructed": bool(first & 0x20), "number": str(number)}
    if node["constructed"]:
        children, position = [], offset
        while position < offset + length:
            child, position = decode_node(data, position, offset + length, depth + 1)
            children.append(child)
        node["children"] = children
    else:
        node["contents"] = data[offset:offset + length].hex()
    return node, offset + length


def decode(hex_octets):
    data = bytes.fromhex(hex_octets)
    if not data:
        raise Reject("input.empty", "no octets")
    node, end = decode_node(data, 0, len(data), 1)
    if end != len(data):
        raise Reject("input.trailing", f"{len(data) - end} octets after the encoding")
    return node


def encode(node):
    number = int(node["number"])
    leading = CLASSES.index(node["class"]) << 6 | (0x20 if node["constructed"] else 0)
    if number < 31:
        identifier = bytes([leading | number])
    else:
        groups = []
        while True:
            groups.insert(0, number & 0x7F)
            number >>= 7
            if not number:
                break
        identifier = bytes([leading | 0x1F] + [group | 0x80 for group in groups[:-1]] + groups[-1:])
    if node["constructed"]:
        contents = b"".join(bytes.fromhex(encode(child)) for child in node["children"])
    else:
        contents = bytes.fromhex(node["contents"])
    size = len(contents)
    if size < 0x80:
        length = bytes([size])
    else:
        octets = size.to_bytes((size.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(octets)]) + octets
    return (identifier + length + contents).hex()


def rejection(error, steps=None):
    result = {"outcome": "reject", "reason": error.reason,
              "diagnostic": {"code": "STUB_" + re.sub(r"[.-]", "_", error.reason).upper(), "message": str(error)}}
    if steps is not None:
        result["steps"] = steps
    return result


def execute(params):
    """Return the result of execute parameters, or raise Unsupported or BadRequest."""
    try:
        standard, request, subject = params["standard"], params["request"], params["input"]
        operation, strictness, view = request["operation"], request["strictness"], request["view"]
        (kind, value), = subject.items()
        supported = (standard["ref"], standard["rules"], strictness, view) == (STANDARD, "der", "strict", "tree")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BadRequest(f"malformed execute parameters: {error}") from error
    if not supported or kind == "value":
        raise Unsupported(f"{standard['ref']} {standard['rules']} {strictness} {view} is not supported")
    try:
        return run(operation, kind, value)
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise BadRequest(f"malformed {kind} input: {error!r}") from error


def run(operation, kind, value):
    if operation == "decode" and kind == "octets":
        try:
            return {"outcome": "accept", "tree": decode(value)}
        except Reject as error:
            return rejection(error)
    if operation == "encode" and kind == "tree":
        return {"outcome": "accept", "octets": encode(value)}
    if operation == "round-trip" and kind == "octets":
        steps = []
        try:
            steps.append({"tree": decode(value)})
            steps.append({"octets": encode(steps[0]["tree"])})
            steps.append({"tree": decode(steps[1]["octets"])})
        except Reject as error:
            return rejection(error, steps)
        return {"outcome": "accept", "steps": steps}
    if operation == "round-trip" and kind == "tree":
        steps = [{"octets": encode(value)}]
        try:
            steps.append({"tree": decode(steps[0]["octets"])})
        except Reject as error:
            return rejection(error, steps)
        return {"outcome": "accept", "steps": steps}
    raise BadRequest(f"{operation} of a {kind} input is not a valid request")


class Stub:
    def __init__(self, args):
        self.args = args
        self.failures = dict(reversed(failure.split("@", 1)) for failure in args.fail)
        self.greeted = False
        self.executed = 0
        self.max_message_octets = 4096
        launch = 1
        if args.launch_file:
            path = Path(args.launch_file)
            launch = int(path.read_text()) + 1 if path.exists() else 1
            path.write_text(str(launch))
        self.version = f"1.0.{launch}" if args.vary_identity else "1.0.0"

    def descriptor(self, protocol):
        return {
            "protocol": protocol,
            "adapter": {"name": "opentlv-corpus-stub-adapter", "version": self.version},
            "implementation": {"name": "stub-der-tree", "version": "1.0.0"},
            "supports": [{"standard": STANDARD, "rules": ["der"], "operations": ["decode", "encode", "round-trip"],
                          "strictness": ["strict"], "views": ["tree"]}],
            "provides": [],
            "limits": {"max_message_octets": 1048576, "max_input_octets": 65536, "max_depth": MAX_DEPTH}}

    def answer(self, line):
        """Return the answer to one request line and the failure mode that applies to it."""
        try:
            message = json.loads(line.decode("utf-8"))
        except ValueError:
            return error(None, "bad-request", "the line is not JSON"), None
        if not isinstance(message, dict):
            return error(None, "bad-request", "the request is not a JSON object"), None
        request_id = message.get("id")
        if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id):
            return error(None, "bad-request", "the request has no valid id"), None
        method, params = message.get("method"), message.get("params")
        if method not in ("hello", "execute"):
            return error(request_id, "bad-request", f"unknown method {method!r}"), None
        if set(message) != {"id", "method", "params"} or not isinstance(params, dict):
            return error(request_id, "bad-request", "a request has exactly an id, a method and object params"), None
        if method == "hello":
            if self.greeted:
                return error(request_id, "bad-request", "hello was already answered"), None
            return self.hello(request_id, params), self.failures.get("hello")
        if not self.greeted:
            return error(request_id, "bad-request", "hello must come first"), None
        self.executed += 1
        mode = self.failures.get(str(self.executed))
        if mode == "internal":
            return error(request_id, "internal-error", "simulated failure"), mode
        if mode == "not-comparable":
            return error(request_id, "not-comparable", "simulated lossy result"), mode
        try:
            return {"id": request_id, "result": execute(params)}, mode
        except Unsupported as reason:
            return error(request_id, "unsupported", str(reason)), mode
        except BadRequest as reason:
            return error(request_id, "bad-request", str(reason)), mode

    def hello(self, request_id, params):
        offered = params.get("protocols")
        if not isinstance(offered, list) or not isinstance(params.get("max_message_octets"), int):
            return error(request_id, "bad-request", "hello needs protocols and max_message_octets")
        common = set(offered) & set(self.args.protocols)
        if not common:
            return error(request_id, "version-unsupported",
                         f"speaks protocols {', '.join(map(str, self.args.protocols))}")
        self.greeted = True
        self.max_message_octets = params["max_message_octets"]
        return {"id": request_id, "result": self.descriptor(max(common))}

    def run(self):
        stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
        for line in iter(stdin.readline, b""):
            if self.args.record:
                with open(self.args.record, "ab") as record:
                    record.write(line)
            reply, mode = self.answer(line)
            if self.args.stderr:
                sys.stderr.write(f"stub: answered {reply.get('id')}\n")
                sys.stderr.flush()
            if mode == "crash":
                sys.stderr.write("stub: simulated crash\n")
                sys.stderr.flush()
                os._exit(3)
            if mode == "hang":
                time.sleep(3600)
            output = write(reply)
            if mode == "garbage":
                output = b"this is not json\n"
            elif mode == "wrong-id":
                output = write(dict(reply, id=reply["id"] + "-other"))
            elif mode == "huge":
                output = b"x" * (self.max_message_octets + 1) + b"\n"
            elif mode == "noise":
                output = b"starting\n" + output
            elif mode == "unsolicited":
                output += b'{"note":"unsolicited"}\n'
            stdout.write(output)
            stdout.flush()
            if mode == "quit":
                sys.exit(0)
        if self.args.ignore_eof:
            time.sleep(3600)
        return self.args.exit_status


def error(request_id, code, message):
    return {"id": request_id, "error": {"code": code, "message": message}}


def write(message):
    return json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"


def failure(text):
    mode, _, when = text.partition("@")
    if mode not in MODES or not (when == "hello" or when.isdigit()):
        raise argparse.ArgumentTypeError(f"{text!r} is not MODE@hello or MODE@N with MODE one of {', '.join(MODES)}")
    return f"{mode}@{when}"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--protocols", type=lambda text: [int(part) for part in text.split(",")], default=PROTOCOLS,
                        help="comma-separated protocol versions it speaks")
    parser.add_argument("--fail", type=failure, action="append", default=[], metavar="MODE@WHEN",
                        help=f"misbehave on one request: {', '.join(MODES)}")
    parser.add_argument("--stderr", action="store_true", help="log every answer to standard error")
    parser.add_argument("--record", help="append every request line it reads to this file")
    parser.add_argument("--launch-file", help="count the launches of the adapter in this file")
    parser.add_argument("--vary-identity", action="store_true", help="report the launch count in the adapter version")
    parser.add_argument("--ignore-eof", action="store_true", help="keep running after standard input ends")
    parser.add_argument("--exit-status", type=int, default=0, help="exit status after standard input ends")
    return Stub(parser.parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
