# Runner and observation log

The runner runs one [resolved execution plan](execution-plan.md#resolved-execution-plan) against one adapter and records what happened in an observation log.
It sends every applicable check to the adapter process under the [adapter process protocol](adapter-protocol.md), one request at a time, and writes down the adapter's answer, how the exchange was classified and what the adapter wrote to its standard error.
It holds no expectation and gives no verdict: the log is the raw evidence a verdict engine evaluates later, and it stays valid when the oracle or the verdict rules change.
This page specifies the runner of [`scripts/run.py`](../scripts/run.py) and schema version 1 of the log:

| Schema | URN | File |
| --- | --- | --- |
| Observation log | `urn:opentlv:corpus:schema:observations:1` | [`schemas/observations-1.schema.json`](../schemas/observations-1.schema.json) |

## Running a plan

```sh
python scripts/run.py --corpus opentlv-corpus-data-0.1.0.tar --sums SHA256SUMS --resolved resolved.json \
  --output observations.jsonl -- <adapter command>
```

| Option | Meaning |
| --- | --- |
| `--corpus`, `--sums` | The data release the plan was resolved against, and optionally its published checksums |
| `--resolved` | The resolved execution plan |
| `--output` | The observation log to create; an existing file is never overwritten |
| `--memory-limit` | Memory each adapter process may use, such as `256M`; see [Bounds](#bounds-and-containment) |
| `--repeat-failures N` | Repeat a crashed, timed-out or protocol-breaking request `N` times in new processes, default 0 |
| `--start-attempts N` | Attempts to start the adapter when it cannot be started, default 3 |

Before the first request the runner verifies the release, starts the adapter, completes the handshake and [checks the resolved plan](execution-plan.md#checking-before-a-run) against the release and the descriptor the adapter answered.
On any problem it writes nothing, prints one `<code>: <detail>` line per problem and exits with status 1:

| Code | Problem |
| --- | --- |
| `output-exists` | The output file exists |
| `release-invalid` | The release archive or its checksums do not verify |
| `json-invalid`, `schema-unsupported`, `schema`, `not-canonicalizable` | The resolved plan cannot be read |
| `adapter-start-failed` | The adapter did not start or did not complete the handshake |
| `corpus-mismatch`, `protocol-unsupported`, `descriptor-invalid`, `adapter-mismatch`, `check-unknown`, `check-order`, `check-mismatch` | The resolved plan does not fit this release or this adapter |

| Exit status | Meaning |
| --- | --- |
| 0 | Every check of the plan has its record |
| 1 | The plan was refused before the first request; no log was written |
| 2 | The session ended early, so some checks were not run |

Crashes, timeouts and protocol errors do not change the exit status: they are observations, and judging them is the verdict engine's task.
At the end the runner prints the counts and the SHA-256 of the log, so a CI job can record exactly which evidence it produced.

## What happens to each check

The runner takes the checks in the order of the resolved plan:

1. A check the plan marks `unsupported` is recorded with its problem and detail and never sent.
2. Any other check is sent as the oracle-blind `execute` request of its assertion, built from the case in the release: standard, type, input and request, and nothing else.
   The case ID, the expectation, the clauses, the required capabilities and the oracle never reach the adapter.
3. The exchange is [classified](adapter-protocol.md#classification), and its record keeps the result or error the adapter answered, verbatim.
4. After a crash, a timeout or a protocol error the harness kills the adapter with everything it started, and starts a new one before the next check; the other checks keep their own records.
   With `--repeat-failures`, the failed request is first repeated in new processes, to show whether the failure reproduces.
   Repetitions are listed with the check and never replace its first execution.
5. When the adapter cannot be started at all, a harness error that says nothing about the implementation, the runner tries again up to `--start-attempts` times.
   When a restarted adapter does not complete the handshake or describes itself differently from the first one, the session ends: every later check is recorded as `not-run` with the reason, never with a result.

A request the harness never sent has no result, and nothing later turns a failure into one.

## Observation log

The log is [JSON Lines](https://jsonlines.org/): one JSON object per line in UTF-8, each matching the observation log schema.
The runner creates the file exclusively, writes each line as soon as it is known and flushes it, and never rewrites a line.
The first line is the run record and the last one the end record; a log without an end record is from a run that was interrupted, and its missing checks were not run.

```json
{"schema": "urn:opentlv:corpus:schema:observations:1", "record": "run", "resolved": "sha256:...", ...}
{"record": "check", "case": "asn1.der.000001", "assertion": "decode", "execution": "completed", "request": "2", "result": {...}, ...}
{"record": "session", "event": "restart", "detail": "the previous exchange failed", "check": "asn1.der.000004/strict"}
{"record": "end", "finished": "...", "complete": true, "checks": 14, "executions": {"completed": 7, "unsupported": 7}, "restarts": 0, "exit_status": 0}
```

### Run record

| Member | Contents |
| --- | --- |
| `resolved` | The identity of the resolved plan that was run |
| `plan`, `corpus`, `adapter` | The plan, release and adapter identities of the resolved plan |
| `tools` | The tooling that ran the plan |
| `descriptor` | The complete capability descriptor the adapter answered |
| `command` | The argument vector the adapter was started with |
| `limits` | The harness limits that were enforced |
| `containment` | How the platform contained the adapter, and the memory limit; see [Bounds](#bounds-and-containment) |
| `retries` | The `repeat_failures` and `start_attempts` of the run |
| `environment` | Operating system, its release, the machine and the Python version |
| `started` | The UTC start time |
| `handshake` | The request ID, duration and standard error of the first handshake |

### Check record

| Member | Contents |
| --- | --- |
| `case`, `assertion`, `digest`, `status` | The check, the definition digest and the lifecycle status from the resolved plan |
| `execution` | The protocol status of the exchange, or `not-run` |
| `problem`, `detail` | Why an exchange did not complete: the protocol problem code, or `session-ended` for `not-run` |
| `request` | The ID of the request, when one was sent |
| `result` | The result the adapter answered: the normalized observation, only for `completed` |
| `error` | The error the adapter answered, verbatim |
| `answer` | The raw answer line of a protocol error, as `text` or, when it is not UTF-8, `hex`; an answer longer than the limit is not kept |
| `stderr` | What the adapter wrote to standard error during the exchange, within the limit |
| `exit_status` | The exit status of an adapter that ended or was killed |
| `duration_ms` | Milliseconds from writing the request to classifying the answer |
| `repetitions` | The repetitions of a failed request, with the same members |

Native diagnostics stay where the adapter put them: a rejection's `diagnostic` inside `result`, and free text in `stderr`.
The `reason` of a rejection is the adapter's mapping to the registered taxonomy; the native code is never turned into a reason.

### Session and end records

A session record is an event of the adapter process outside the classification of a request: `restart`, `unsolicited-output`, `exited` between requests, `exit-timeout`, a non-zero `exit-status` at shutdown, `start-failed` or `adapter-changed`, with a detail, the check being run and, for a failed start, its standard error.

The end record counts the checks by `execution`, the restarts and the exit status of the last adapter process, and says whether the run is `complete`.

### Reproducibility

The results, errors, classifications and request IDs of a deterministic adapter are the same in every run of the same resolved plan.
Times, durations, exit statuses of killed processes and standard error are not, and the environment differs between machines, so two logs are compared by their check records without those members, never by their bytes.

## Bounds and containment

Every adapter process is contained with what the operating system offers:

| Bound | Linux and other POSIX systems | macOS | Windows |
| --- | --- | --- | --- |
| Handshake, request and exit time | Enforced | Enforced | Enforced |
| Answer line and standard error size | Enforced | Enforced | Enforced |
| Memory, `--memory-limit` | `RLIMIT_AS` | Not enforced | Job object process memory |
| Killing every process the adapter started | Process group | Process group | Job object |
| Crash handling | No core dump | No core dump | No error reporting dialog |

- The time and size limits come from the resolved plan, so they are part of what was resolved.
  The memory limit is a property of the machine and is recorded in the run record, with what enforced it: `rlimit-as`, `job-object`, `unenforced` or `none`.
- Exhausting the memory limit makes the adapter's allocations fail.
  The implementation then crashes, which is recorded as `crashed`, or answers what it answers when an allocation fails.
- `RLIMIT_AS` limits address space, not resident memory, so sanitizers and runtimes that reserve large address ranges need a higher limit or none.
- macOS accepts `RLIMIT_AS` but does not enforce it; the runner warns and records `unenforced`.
- On POSIX systems a process that leaves the adapter's process group, for example with `setsid`, escapes the kill.
  On Windows a process the adapter starts in the instant before the harness assigns it to the job escapes it.
- The harness runs the adapter without a shell, reads its standard error continuously and never blocks on a full pipe, so a hung adapter only costs the request time limit.

A crash, timeout or memory exhaustion is recorded against the implementation and its adapter; it is not by itself a standards violation.

## Platforms and CI

The corpus CI runs the runner and protocol tests with the [stub adapter](../tests/stub_adapter.py) on Linux, Windows and macOS: smoke and full plans, timeouts, crashes, malformed and oversized answers, restarts, changed adapters, repetitions, infrastructure retries, the memory limit and killing of the process tree.

The OpenTLV Core adapter is built and maintained in [OpenTLV TestLab](https://github.com/OpenTLV/opentlv-testlab), using only the public OpenTLV API.
Neither the corpus nor OpenTLV Core depends on it: TestLab consumes both.
To run the corpus tests against it, name its command in `OPENTLV_CORPUS_CORE_ADAPTER`:

```sh
OPENTLV_CORPUS_CORE_ADAPTER=/path/to/opentlv-corpus-adapter python -m unittest discover -s tests -p test_run.py -v
```

The test resolves the smoke plan against the adapter's descriptor, runs it and requires every applicable check to complete.
