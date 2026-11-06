# opentlv-corpus

OpenTLV Corpus — An open, reusable collection of TLV test vectors, conformance cases, and reference data for validating, benchmarking, and comparing TLV implementations across languages and libraries.

Every case states what a standard requires for one input, with an independent oracle: cited clauses, a published test vector or the corpus operation contract.
Agreement between implementations is never treated as proof.
The cases are language-neutral data, usable by OpenTLV Core, its bindings, OpenTLV TestLab and unrelated TLV implementations without Python and without any third-party reference library.

## Status

The corpus is in its foundation phase.
The test case, registry, lifecycle and release manifest schemas, the ID and namespace rules, the oracle contract, the canonical definition digest, the corpus validator, the data-only release, the adapter process protocol and execution plans with their deterministic resolution are defined; conformance cases and the runner follow.
Schema version 1 and protocol version 1 are provisional until the first stable corpus release.

## Layout

| Path | Contents |
| --- | --- |
| [`schemas/`](schemas) | JSON Schemas (draft 2020-12) of the test case, registry, lifecycle log, release manifest, execution plan and resolved execution plan documents |
| [`registry/registry.json`](registry/registry.json) | Standard editions, ID namespaces and their allocations, rejection reasons, capabilities, licenses and shared suites such as `smoke` |
| [`registry/lifecycle.json`](registry/lifecycle.json) | Append-only lifecycle log of the conformance cases |
| [`examples/valid/`](examples/valid) | Example cases covering every construct of the schema |
| [`examples/invalid.json`](examples/invalid.json) | Mutations of the valid examples, each breaking exactly one rule |
| [`examples/lifecycle.json`](examples/lifecycle.json) | Lifecycle log of the examples; `lifecycle-projection.json` and `lifecycle-invalid.json` hold its projection and invalid mutations |
| [`examples/digests.json`](examples/digests.json) | Canonicalization and definition digest vectors |
| [`examples/plans/`](examples/plans) | Example execution plans: full, smoke, filtered and explicit IDs |
| [`scripts/check_contract.py`](scripts/check_contract.py) | Checks the rules the schemas cannot express and builds oracle-blind adapter views |
| [`scripts/canonical.py`](scripts/canonical.py) | RFC 8785 canonical JSON and definition digests |
| [`scripts/check_lifecycle.py`](scripts/check_lifecycle.py) | Replays and checks the lifecycle logs and checks the digest vectors |
| [`scripts/check_history.py`](scripts/check_history.py) | Checks a change against its base revision for rewritten history |
| [`scripts/validate.py`](scripts/validate.py) | Validates a corpus tree, the repository or an extracted release, with deterministic diagnostics |
| [`scripts/release.py`](scripts/release.py) | Builds and verifies the reproducible data-only release |
| [`scripts/protocol.py`](scripts/protocol.py) | Harness side of the adapter process protocol: framing, classification, sessions and restarts |
| [`scripts/check_adapter.py`](scripts/check_adapter.py) | Checks that an adapter follows the adapter process protocol |
| [`scripts/plan.py`](scripts/plan.py) | Execution plan model, resolution against a release and an adapter, and the check before a run |
| [`protocol/`](protocol) | JSON Schemas of the adapter protocol requests, responses and capability descriptor, and golden transcripts |
| [`tests/stub_adapter.py`](tests/stub_adapter.py) | Minimal adapter, standard library only, that the protocol tests and transcripts use |
| `releases/` | Manifests and checksums of published releases, never changed |
| [`docs/`](docs) | Specifications |

## Specifications

- [Test case and oracle contract](docs/test-case-contract.md): normative and informative fields, inputs, operations, strictness, expectations, canonical representations, rejection reasons, oracle evidence and oracle-blind requests.
- [Identity](docs/identity.md): ID grammar, namespace and serial allocation, no reuse, and the change policy before and after the first stable release.
- [Lifecycle](docs/lifecycle.md): append-only lifecycle events, statuses, replacements, the 0.x and stable phases, the projection and tainted results.
- [Canonical definition digest](docs/digest.md): RFC 8785 canonicalization, SHA-256, digest scope, byte encoding and limits.
- [Corpus validation](docs/validation.md): what the validator checks and its diagnostic format.
- [Data release](docs/release.md): the data-only archive, its manifest and checksums, reproducibility, verification without Python and published release records.
- [Adapter process protocol](docs/adapter-protocol.md): JSON Lines over standard streams, handshake and capability descriptor, oracle-blind requests, normalized observations, limits, lifecycle and restarts, failure classification and retries.
- [Execution plans](docs/execution-plan.md): declarative selection, shared suites, deterministic resolution against a pinned release and an adapter, applicability decisions and the check before a run.
- [Schema compatibility](docs/compatibility.md): schema versions and how they relate to data, tooling and protocol versions.

## Checks

Python 3.11 or later:

```sh
python -m pip install -r requirements-dev.txt
python scripts/check_contract.py
python scripts/check_lifecycle.py
python scripts/validate.py
python scripts/check_history.py --base origin/main
python -m unittest discover -s tests -v
```

Check an adapter against the protocol:

```sh
python scripts/check_adapter.py -- python tests/stub_adapter.py
```

Build and verify a data release:

```sh
python scripts/release.py build --version 0.1.0
python scripts/release.py verify build/release/opentlv-corpus-data-0.1.0.tar --sums build/release/SHA256SUMS
```

Resolve an execution plan against a release and an adapter, and check it before a run:

```sh
python scripts/plan.py resolve --corpus build/release/opentlv-corpus-data-0.1.0.tar --plan examples/plans/smoke.json   --output build/resolved.json -- python tests/stub_adapter.py
python scripts/plan.py check --corpus build/release/opentlv-corpus-data-0.1.0.tar --resolved build/resolved.json   -- python tests/stub_adapter.py
```

## License

[MIT](LICENSE)
