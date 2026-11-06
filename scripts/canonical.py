# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Marek Cingel

"""Canonical JSON (RFC 8785) and the definition digest of a test case.

Corpus documents hash only JSON that needs no floating-point formatting:
integers within the I-JSON range, strings of Unicode scalar values, booleans,
null, arrays and objects. For that subset the JSON Canonicalization Scheme
writes every integer as a plain decimal, so this module implements it without
a number formatter and rejects anything outside the subset instead of
guessing.

Run it with case files to print their definition digests.
"""

import argparse
import hashlib
import json
import sys

MAX_INTEGER = 2 ** 53 - 1
DIGEST_SCOPE = ("schema", "id", "normative")
DIGEST_PREFIX = "sha256:"

_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


class CanonicalError(ValueError):
    """The value is outside the JSON subset the corpus canonicalizes."""


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalError(f"duplicate member {key!r}")
        result[key] = value
    return result


def _reject_constant(name):
    raise CanonicalError(f"{name} is not JSON")


def loads(text):
    """Parse JSON text, rejecting duplicate members and NaN or Infinity.

    A document with a repeated member means different things to different
    parsers, so its digest would depend on the consumer.
    """
    return json.loads(text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant)


def load_json(path):
    with open(path, encoding="utf-8") as stream:
        return loads(stream.read())


def _string(text, path):
    if any(0xD800 <= ord(char) <= 0xDFFF for char in text):
        raise CanonicalError(f"{path}: lone surrogate in a string")
    parts = []
    for char in text:
        if char in _ESCAPES:
            parts.append(_ESCAPES[char])
        elif char < " ":
            parts.append(f"\\u{ord(char):04x}")
        else:
            parts.append(char)
    return '"' + "".join(parts) + '"'


def _sort_key(name):
    # RFC 8785 orders members by their UTF-16 code units. Comparing big-endian
    # UTF-16 bytes is the same comparison; plain code point order differs once
    # a name has characters above U+FFFF.
    return name.encode("utf-16-be")


def _serialize(value, path, parts):
    if value is None:
        parts.append("null")
    elif value is True:
        parts.append("true")
    elif value is False:
        parts.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_INTEGER:
            raise CanonicalError(f"{path}: integer {value} is outside -(2^53 - 1) to 2^53 - 1")
        parts.append(str(value))
    elif isinstance(value, float):
        raise CanonicalError(f"{path}: floating-point number {value!r}")
    elif isinstance(value, str):
        parts.append(_string(value, path))
    elif isinstance(value, (list, tuple)):
        parts.append("[")
        for index, item in enumerate(value):
            if index:
                parts.append(",")
            _serialize(item, f"{path}/{index}", parts)
        parts.append("]")
    elif isinstance(value, dict):
        for name in value:
            if not isinstance(name, str):
                raise CanonicalError(f"{path}: member name {name!r} is not a string")
            _string(name, path)
        parts.append("{")
        for index, name in enumerate(sorted(value, key=_sort_key)):
            if index:
                parts.append(",")
            parts.append(_string(name, path))
            parts.append(":")
            _serialize(value[name], f"{path}/{name}", parts)
        parts.append("}")
    else:
        raise CanonicalError(f"{path}: {type(value).__name__} is not JSON")


def canonicalize(value):
    """Return the RFC 8785 serialization of a value as text."""
    parts = []
    _serialize(value, "", parts)
    return "".join(parts)


def canonical_bytes(value):
    """Return the RFC 8785 serialization of a value as UTF-8 octets."""
    return canonicalize(value).encode("utf-8")


def definition(case):
    """Return the part of a case the digest covers: schema, id and normative.

    The scope is an allow list, so informative metadata, and any member added
    to a case later, never changes the digest unless the contract admits it.
    """
    return {name: case[name] for name in DIGEST_SCOPE}


def digest(case):
    """Return the canonical definition digest of a case, sha256:<hex>."""
    return DIGEST_PREFIX + hashlib.sha256(canonical_bytes(definition(case))).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="+", help="test case files")
    args = parser.parse_args(argv)
    status = 0
    for path in args.cases:
        try:
            print(f"{digest(load_json(path))}  {path}")
        except (OSError, ValueError, KeyError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
