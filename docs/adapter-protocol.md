# Adapter process protocol

An adapter connects one TLV implementation to the corpus harness.
It runs as a separate process and exchanges JSON Lines with the harness over its standard streams.
This page specifies protocol version 1, defined by three schemas:

| Schema | URN | File |
| --- | --- | --- |
| Request | `urn:opentlv:corpus:protocol:request:1` | [`protocol/request-1.schema.json`](../protocol/request-1.schema.json) |
| Response | `urn:opentlv:corpus:protocol:response:1` | [`protocol/response-1.schema.json`](../protocol/response-1.schema.json) |
| Capability descriptor | `urn:opentlv:corpus:protocol:capabilities:1` | [`protocol/capabilities-1.schema.json`](../protocol/capabilities-1.schema.json) |

An adapter only needs to read and write lines of JSON, so it can be written in any language, and neither it nor the implementation needs Python.
[`tests/stub_adapter.py`](../tests/stub_adapter.py) is a minimal adapter that uses only the Python standard library; the tests and the [golden transcripts](#golden-transcripts) run it.
[`scripts/protocol.py`](../scripts/protocol.py) is the harness side, and [`scripts/check_adapter.py`](../scripts/check_adapter.py) [checks an adapter](#conformance-check) against this page.

## Principles

- **Out of process.** Every adapter runs in its own process, including one written in Python.
  A crash, hang or memory fault of the implementation ends at most that process, never the harness or the results of other requests.
- **Oracle-blind.** An adapter receives the input and the execution parameters of one assertion and nothing else.
  It never receives the case ID, expectations, expected reasons, clauses, required capabilities, oracle or review information, verdicts or the results of other implementations.
  It reports what the implementation does; only the verdict engine compares that with the expectation.
- **One request at a time.** The harness sends a request only after the previous one is answered or has failed, so every answer, crash and timeout belongs to exactly one request.
- **No invented results.** A request without a valid answer has no result.
  The harness records why, and nothing that happens later turns the failure into a result.

## Transport

- The harness starts the adapter from an argument vector, without a shell, with its standard input, output and error connected to pipes.
- Each message is one JSON object ([RFC 8259](https://www.rfc-editor.org/rfc/rfc8259)) in UTF-8 on one line that ends with a line feed (LF).
  A serializer that writes no line breaks inside the object produces such a line, because JSON escapes line feeds in strings.
- Standard output carries only answers: no banner, prompt, progress or log line.
- The harness writes compact JSON in ASCII, escaping other characters, with the members in any order.
  Adapters accept any valid JSON object: whitespace, escapes and member order may vary.
- Adapters write UTF-8 without a byte order mark.
  A carriage return before the line feed is JSON whitespace and accepted, so text-mode output on Windows works.
  Member names do not repeat, and `NaN` and `Infinity` are not JSON.
- Each side declares the longest message line it reads, excluding the line feed: the harness in [`hello`](#hello), the adapter in its [capability descriptor](#capability-descriptor).
  Both are at least 4096 octets.
- Standard error is free text for people and is never parsed.
  The harness reads it continuously, so the adapter never blocks on a full pipe, and records what arrives during a request as that request's native diagnostics, up to a [limit](#limits).
  Standard error is read concurrently with the answer, so text written just before an answer may be recorded with the next request; after a crash or timeout it is complete.

## Messages

A request names a method and its parameters:

```json
{"id": "7", "method": "execute", "params": {}}
```

| Member | Contents |
| --- | --- |
| `id` | 1 to 64 letters, digits, `.`, `_` or `-`. The harness never reuses an ID within a run, including across restarts. The adapter treats it as opaque and echoes it. |
| `method` | [`hello`](#hello) or [`execute`](#execute) |
| `params` | The parameters of the method |

The answer has exactly one of `result` and `error`:

```json
{"id": "7", "result": {"outcome": "accept", "octets": "020105"}}
{"id": "7", "error": {"code": "unsupported", "message": "typed values are not supported"}}
```

`id` echoes the request.
It is `null` only in a `bad-request` answer to a line whose ID the adapter cannot read.
`message` is at most 4096 characters for people and is never interpreted.

| Error code | Methods | Meaning |
| --- | --- | --- |
| `version-unsupported` | `hello` | The adapter speaks none of the offered protocol versions. |
| `bad-request` | Both | The line is not a valid request, or comes out of order: an `execute` before a completed `hello`, or a second `hello`. |
| `unsupported` | `execute` | The request is valid, but the adapter or the implementation cannot execute it, such as an unsupported type kind or a declared limit the input exceeds. |
| `not-comparable` | `execute` | The implementation executed the request, but its result cannot be represented exactly in the normalized form, for example because its tree API merges segments of a constructed string. |
| `internal-error` | Both | An unexpected failure inside the adapter or the implementation, such as an exception that is not a rejection, after which the adapter is still in a consistent state. |

An adapter answers `bad-request` to lines that are not JSON objects, lack a valid `id`, `method` or object `params`, name an unknown method or arrive out of order.
It may also answer `bad-request` to parameters that do not match the request schema; the harness never sends those.

### hello

The first request to every adapter process is `hello`:

```json
{"id": "1", "method": "hello", "params": {"protocols": [1], "max_message_octets": 4194304}}
```

| Parameter | Contents |
| --- | --- |
| `protocols` | The protocol versions the harness speaks |
| `max_message_octets` | The longest answer line the harness reads, excluding the line feed |

The adapter answers with its capability descriptor in the highest version both speak, or with `version-unsupported`.
The `hello` request has this form in every protocol version, so any adapter can answer any harness.

### Capability descriptor

```json
{
  "protocol": 1,
  "adapter": {"name": "example-adapter", "version": "1.0.0"},
  "implementation": {"name": "example-tlv", "version": "2.3.1"},
  "supports": [
    {"standard": "itu-t-x690-2021-02", "rules": ["ber", "der"], "operations": ["decode", "encode", "round-trip"],
     "strictness": ["strict"], "views": ["tree"]}
  ],
  "provides": ["tag-number.over-31-bits"],
  "limits": {"max_message_octets": 1048576, "max_input_octets": 65536, "max_depth": 64}
}
```

| Member | Contents |
| --- | --- |
| `protocol` | The selected protocol version |
| `adapter` | Name and version of the adapter program |
| `implementation` | Name and version of the implementation under test |
| `supports` | Combinations of [standard edition](test-case-contract.md#standard), rules, operations, strictness and views the adapter executes. A request is covered when one entry contains its standard, rules, operation, strictness and view. |
| `provides` | [Registered capabilities](test-case-contract.md#clauses-and-capabilities) the implementation has, such as `tag-number.over-31-bits` |
| `limits.max_message_octets` | The longest request line the adapter reads, excluding the line feed |
| `limits.max_input_octets` | Optional: the longest octets input the implementation accepts |
| `limits.max_depth` | Optional: the deepest nesting of TLV trees and typed values the implementation handles, where a primitive has depth 1 |

The harness does not send a request the descriptor does not cover; it records the request as [`unsupported`](#classification) instead.
The descriptor is a claim, not a promise the harness relies on: it still checks every answer, and reports keep the requests the adapter answered `unsupported` although its descriptor covers them.

### execute

The parameters are the [oracle-blind view](test-case-contract.md#oracle-blind-requests) of one assertion, in the representations of the [test case contract](test-case-contract.md#canonical-representations):

```json
{"id": "2", "method": "execute", "params": {
  "standard": {"ref": "itu-t-x690-2021-02", "rules": "der"},
  "request": {"operation": "decode", "strictness": "strict", "view": "tree"},
  "input": {"octets": "020105"}}}
```

| Parameter | Contents |
| --- | --- |
| `standard` | Standard edition and rules |
| `type` | The ASN.1 type, when the input is a typed value or the view is `value` |
| `input` | The case input: `octets`, `tree` or `value` |
| `request` | Operation, strictness and view |

The result is a normalized observation of what the implementation did:

| Operation | Accepted | Contents |
| --- | --- | --- |
| `decode` | `{"outcome": "accept", "tree": node}` | The decoded tree, in the tree view |
| `decode` | `{"outcome": "accept", "value": value}` | The decoded value, in the value view |
| `encode` | `{"outcome": "accept", "octets": hex}` | The encoding |
| `round-trip` | `{"outcome": "accept", "steps": [...]}` | The output of every step |

Any operation can be rejected:

```json
{"outcome": "reject", "reason": "length.non-minimal", "diagnostic": {"code": "E_LENGTH", "message": "non-minimal length at offset 1"}}
```

- `reason` is a [registered reason code](test-case-contract.md#rejection-reasons) the adapter mapped the native error to.
  Without a mapping it is absent, and the reason is not compared.
  It is never a native error code.
- `diagnostic` holds the native error code, message or both, which reports keep verbatim.
  An accepted result may carry one too, for a warning.

A round trip lists the output of each step, not whether anything was preserved; the verdict engine compares the steps with the input and with each other:

| Input | Steps |
| --- | --- |
| `octets` | Decode in the requested view, encode that result, decode the encoding again |
| `tree` or `value` | Encode, decode in the requested view |

Each step is `{"tree": node}`, `{"value": value}` or `{"octets": hex}`.
An accepted round trip lists every step.
A rejected one lists the steps completed before the one that failed, possibly none.

An `execute` answer depends only on its parameters.
The adapter keeps no state between requests that changes a result, so the same parameters always give the same answer.
The harness may send requests in any order and may restart the adapter between any two.

## Lifecycle

1. **Start.** The harness starts the adapter process.
2. **Handshake.** It sends `hello` and waits for the descriptor.
3. **Requests.** It sends `execute` requests, one at a time.
4. **Shutdown.** It closes the adapter's standard input.
   The adapter exits with status 0 once it has read the end of its input.
   An adapter still running after the exit grace period is killed.
   A non-zero exit status, or an exit timeout, is recorded for the session, not for a request: every request was answered before.

The harness kills the adapter, with its process group where the platform has one, and starts a new one before the next request:

- after a `timeout`, `protocol-error` or `harness-error`;
- after a `crashed` request, except an `internal-error` answer, after which the adapter is still in a consistent state;
- when the adapter wrote output that answers no request, or ended between requests; this is recorded for the session.

A restart starts a new process and repeats the handshake.
The new descriptor must equal the first one; otherwise the session ends with `adapter-changed`, so the results of two different adapters never mix in one run.
When the adapter cannot be started or does not complete the handshake, the session ends.
A request the harness never sent has no observation and is reported as not run, never with a result.

### Limits

The harness enforces these limits; a runner configures them and records the values it used with the observations.

| Limit | Default | Scope |
| --- | --- | --- |
| Handshake time | 10 s | From writing `hello` to reading the complete answer line |
| Request time | 10 s | From writing an `execute` request to reading the complete answer line |
| Exit grace period | 5 s | From closing standard input to the exit of the adapter |
| Answer size | 4 MiB | `max_message_octets` of `hello` |
| Standard error | 64 KiB per request | Text beyond it is dropped and the record marked as truncated |

The protocol carries no memory limit.
A runner may limit the adapter's memory with operating system facilities; exhausting it shows up as `crashed`, or as whatever the implementation answers when an allocation fails.

## Classification

Every request ends in exactly one status:

| Status | Cause | Result | Restart |
| --- | --- | --- | --- |
| `completed` | A valid answer with a result | The observation | No |
| `unsupported` | The descriptor does not cover the request, which is then not sent, or the adapter answers `unsupported` or `version-unsupported` | None | No |
| `not-comparable` | The adapter answers `not-comparable` | None | No |
| `bad-request` | The adapter answers `bad-request` | None | No |
| `protocol-error` | The answer breaks this protocol | None | Yes |
| `crashed` | The adapter's output ended before the answer, writing the request failed, or the adapter answers `internal-error` | None | Yes, except after `internal-error` |
| `timeout` | No complete answer line within the time limit | None | Yes |
| `harness-error` | The harness failed, for example it cannot start the adapter | None | Yes |

`unsupported` and `not-comparable` decide applicability; the other failures are execution statuses.
Only `completed` requests have an observation for the verdict engine.
A crash or timeout is recorded against the implementation and its adapter, but it is not by itself a standards violation.
`bad-request` and `protocol-error` show a defect of the adapter or the harness, never a result of the implementation.

Each status other than `completed` has a problem code that tells why:

| Status | Problem | Meaning |
| --- | --- | --- |
| `unsupported` | `undeclared` | No `supports` entry covers the standard, rules, operation, strictness and view; not sent |
| `unsupported` | `capability` | The assertion requires a capability the descriptor does not provide; not sent |
| `unsupported` | `input-too-large` | The octets input is longer than `max_input_octets`; not sent |
| `unsupported` | `too-deep` | The tree or value input is nested deeper than `max_depth`; not sent |
| `unsupported` | `message-too-large` | The request line is longer than the adapter's `max_message_octets`; not sent |
| `unsupported` | `unsupported`, `version-unsupported` | The adapter's answer |
| `not-comparable`, `bad-request` | The error code | The adapter's answer |
| `protocol-error` | `message-too-large` | The answer line is longer than the harness's `max_message_octets` |
| `protocol-error` | `encoding-invalid` | The answer is not UTF-8 |
| `protocol-error` | `json-invalid` | The line is empty, is not one JSON text, repeats a member name or uses `NaN` or `Infinity` |
| `protocol-error` | `schema` | The answer does not match the response schema for the method |
| `protocol-error` | `id-mismatch` | The answer names another request, or `null` for a request the harness sent |
| `protocol-error` | `answer-mismatch` | The answer does not fit the request: an error code the method cannot answer, or a result whose form does not fit the operation, view and input |
| `crashed` | `exited` | The adapter's output ended before the answer; its exit status is recorded |
| `crashed` | `write-failed` | The adapter stopped reading before it received the request |
| `crashed` | `internal-error` | The adapter's answer |
| `timeout` | None | No complete answer line in time; the process is killed |
| `harness-error` | `spawn-failed` | The adapter process cannot be started |

### Retries

- `completed`, `unsupported`, `not-comparable` and `bad-request` are never retried: the answer is the answer.
- `crashed`, `timeout` and `protocol-error` are final for the request.
  The harness never sends the request again to replace the failure with a result.
  A runner may repeat it in a new process to learn whether the failure reproduces; each repetition is recorded as an exchange of its own, and the first one stays the status of the request.
- `harness-error` says nothing about the implementation, so a runner may retry the request in a new process a bounded number of times, recording every attempt.

## Golden transcripts

[`protocol/transcripts/`](../protocol/transcripts) holds complete exchanges with the stub adapter, one JSON event per line:

| Event | Meaning |
| --- | --- |
| `{"title": ..., "stub_arguments": [...]}` | First line: what the transcript shows, and the stub adapter's arguments |
| `{"send": message}` | The harness writes this request |
| `{"send_raw": text, "method": ..., "id": ...}` | The harness writes this text as a line; the answer is classified as one to this method and ID |
| `{"receive": message, "status": ...}` | The adapter answers with this message, which the harness classifies with this status |
| `{"close": true}` | The harness closes the adapter's standard input |
| `{"exit": status}` | The adapter exits with this status |

| Transcript | Shows |
| --- | --- |
| [`session.jsonl`](../protocol/transcripts/session.jsonl) | Handshake, decode, encode and round trips of octets and trees, rejections with reasons and native diagnostics, an adapter that answers `unsupported`, and shutdown |
| [`bad-requests.jsonl`](../protocol/transcripts/bad-requests.jsonl) | `bad-request` answers to a request before the handshake, malformed lines, an unknown method, missing parameters and a second `hello`, after which the adapter still answers |
| [`version-unsupported.jsonl`](../protocol/transcripts/version-unsupported.jsonl) | An adapter that shares no protocol version with the harness |

The tests validate every message against the schemas, replay each transcript against the stub adapter and require the same answers, statuses and exit status.
An adapter of another implementation answers with its own identity and results, and its own error messages.

## Conformance check

```sh
python scripts/check_adapter.py -- <command> [<argument> ...]
```

The check starts the adapter and runs one fixed sequence:

1. an `execute` before the handshake, which must be answered `bad-request`;
2. the handshake, which must complete with a valid descriptor;
3. malformed lines, an unknown method, a request without parameters and a second `hello`, each answered `bad-request`;
4. every [example](../examples/valid) assertion the descriptor covers, each answered without `bad-request` or a protocol error;
5. the first completed request again, which must give the same result;
6. the shutdown, after which the adapter must exit with status 0 within the grace period.

Output that answers no request and an adapter that ends between requests are problems too.

It checks the protocol, not the implementation: it never compares a result with an expectation, and it counts crashes and timeouts of the implementation without reporting them as problems.
Each problem is one line, `<step>: <code>: <detail>`, and the check exits with status 1 when it reports any.
`--timeout` sets the time limit of each request.

## Versioning

- The protocol version is an integer, independent of the schema, data release and tooling versions, as [Schema compatibility](compatibility.md#independent-versions) describes.
  A protocol version is identified by the URNs of its three schemas.
- Every schema is closed: unknown members are rejected, in requests and answers alike, so no expectation can be slipped into a request.
- Any change that makes a valid message invalid, makes an invalid message valid, or changes the meaning of a field creates a new protocol version with new URNs and files.
  The previous version stays as long as a supported harness or adapter uses it, and `hello` negotiates between them.
- During 0.x, version 1 is provisional and may change in place in a reviewed pull request that records the change in the CHANGELOG.
- The request schema contains copies of the tree, value, type, input, request and standard definitions of the [test case schema](../schemas/case-1.schema.json) version 1, so it stands alone and its version does not move with the case schema.
  The tests keep the copies identical.
