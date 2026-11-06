# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Execution plans (schema version 1): declarative selection that never depends on results, a shared `smoke` suite in the registry, and deterministic resolution against a pinned release and an adapter into a resolved plan with exact checks, digests and applicability, checked before every run. (#6)
- Adapter process protocol version 1 (JSON Lines): handshake and capability descriptor, oracle-blind requests without case IDs, normalized observations, failure classification, limits and restart rules; schemas, golden transcripts, a stub adapter and an adapter conformance check. (#5)
- Corpus validator with deterministic diagnostics, a reproducible data-only release (ustar archive, manifest schema, SHA-256 sums) readable without Python, immutable release records and a registered license vocabulary for case data. (#4)
- Append-only lifecycle log (deprecate, invalidate, reinstate, revise) with replacement and cycle checks, a reproducible projection of tainted definitions and a CI history check; RFC 8785 and SHA-256 definition digests with vectors. (#3)
- Test case and registry schemas (version 1), ID grammar and namespace allocation, the 0.x and stable change policy, and the independent oracle contract. Cases separate normative content from informative metadata and support decode, encode and round-trip assertions with strictness, permitted alternatives, implementation-defined results and a reason taxonomy; adapters see only oracle-blind requests. Valid and invalid examples, contract checks and a PR workflow enforce it. (#2)
