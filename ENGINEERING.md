# Stram Quant MCP engineering standards

These rules apply to source code, tests, configuration, documentation, reviews, release artifacts,
and automation in this repository. They are normative: **MUST** is required, **SHOULD** is the default
unless a documented reason justifies an exception, and **MAY** is optional.

The order of priorities is: **security and data correctness, reliability, maintainability,
operability, then delivery speed**. Performance optimization comes after measurement.

## 1. Core engineering principles

1. **Keep the Tool Plane thin.** The service MUST delegate quantitative behavior to the pinned public
   `stram_data.data_interface`; it MUST NOT duplicate SQL, financial calculations, ingestion,
   alignment, lineage, revision, or availability logic.
2. **Fail closed at trust boundaries.** Missing or ambiguous identity, authorization, tenant, schema,
   policy, audit, or readiness state MUST deny the operation. Internal failures MUST NOT silently
   degrade security or data semantics.
3. **Make invalid states difficult to represent.** Use typed, immutable models, closed schemas, enums,
   constrained values, and explicit state transitions instead of loosely structured dictionaries and
   sentinel strings.
4. **Prefer the simplest complete design.** Add an abstraction, dependency, service, cache, retry, or
   configuration option only for a current, tested need. Three similar lines are often safer than an
   unproven framework.
5. **Be explicit.** Defaults affecting security or correctness MUST be visible, tested, documented, and
   included in the policy/configuration version. In particular, `strict` availability is the default.
6. **Make changes small and reversible.** One milestone or concern per change. Preserve unrelated work,
   avoid broad refactors, and keep rollback possible without Data Plane mutation.
7. **Prefer deterministic behavior.** Given the same request, identity, policy, pinned release, and lake
   state, the service SHOULD produce the same result and audit classification.
8. **Preserve epistemic status.** Never label estimates as verified, truncation as completeness,
   readiness as end-to-end success, or a smoke test as production qualification.
9. **Automate repeatable checks.** If a rule can be checked reliably in CI, it SHOULD be automated.
   Automation MUST report what it established and MUST NOT manufacture success.

## 2. Ownership and dependency boundaries

The allowed dependency direction is:

```text
transport -> authentication/policy -> tool application layer -> worker boundary
           -> public stram_data.data_interface -> read-only lake
```

- Imports from `stram_data` MUST come only from `stram_data.data_interface` or the top-level package
  version. Internal `stram_data.*` modules are forbidden.
- Transport models MUST NOT become domain models. Tool handlers translate validated MCP inputs into
  public Data Interface request models and serialize public results; they contain no data logic.
- Authentication, authorization, rate policy, audit, and telemetry MUST be independently testable
  components with explicit interfaces. Authorization MUST be enforced at dispatch, not in tool text.
- The query worker MUST NOT know bearer tokens or authorization headers. The HTTP process MUST NOT
  reach around the worker into DuckDB or the lake.
- Modules MUST have one clear responsibility and no circular imports. Cross-cutting dependencies
  SHOULD be passed at application construction rather than fetched from mutable globals.
- Runtime configuration MUST be parsed once into an immutable typed object. Code MUST NOT scatter
  direct environment-variable reads throughout the application.
- There MUST be no hidden network, filesystem, clock, randomness, or process side effects in model
  construction or module import.

## 3. Python and code-quality rules

- Python 3.12+ and `uv` are the supported toolchain. Dependencies and development tools MUST be declared
  in `pyproject.toml` and fully resolved in `uv.lock`.
- Ruff formatting/linting and strict mypy MUST pass. New code MUST be typed. `Any`, casts,
  `# type: ignore`, and `# noqa` require the narrowest possible scope and a nearby reason.
- Public interfaces, security invariants, non-obvious failure behavior, and important units MUST have
  concise docstrings. Comments explain **why**, constraints, or evidence; they do not narrate syntax.
- Functions and classes SHOULD be small and cohesive. Deep nesting, high branch complexity, boolean
  flag combinations, and long parameter lists SHOULD be replaced with named types or smaller units.
- Prefer immutable Pydantic models, dataclasses, tuples, and read-only mappings at boundaries. Do not
  use mutable default arguments or return internal mutable state.
- Use timezone-aware UTC timestamps internally and ISO 8601 at boundaries. Never depend on host local
  time. Preserve `Decimal` as a decimal string where the Data Interface does so; do not round silently.
- Use typed exceptions for expected failure categories. Broad exception handling is allowed only at a
  process/request boundary, where it MUST preserve cancellation, log safe diagnostics, and return a
  generic error. Never use `except:`, suppress an error without evidence, or parse exception text as a
  contract.
- `assert` MUST NOT validate untrusted input or enforce runtime security. Validation and policy checks
  use explicit code and typed failures.
- Application code MUST use structured logging, not `print`. Logs MUST use stable event names and safe,
  bounded fields; never interpolate raw requests, results, exceptions, or tokens.
- Collections, queues, recursion, payloads, strings, concurrency, retries, and time spent MUST be
  bounded. No unbounded `gather`, thread pool, cache, queue, or result accumulation.
- Blocking Data Interface calls MUST stay behind the worker boundary. Async request handlers MUST NOT
  block the event loop.
- Dead code, commented-out code, speculative hooks, unused feature flags, and placeholder TODOs without
  an owner/reason MUST be removed. A future idea belongs in `PLANS.md`, not in production code.
- Public names and configuration keys MUST be clear and domain-specific. Avoid opaque abbreviations,
  generic `utils.py` dumping grounds, and misleading terms such as “verified” for modeled data.

### 3.1 Readability and structure rules

These rules make the code understandable to a new reader. They are normative for new and changed code.
Existing code is not refactored for its own sake: its violations are recorded in the lint baseline in
`pyproject.toml` (see [Baseline and ratchet](#33-baseline-and-ratchet)), which may only shrink.

| # | Rule | Enforcement |
|---|---|---|
| R1 | A function MUST be at most 50 statements and SHOULD fit in about 40 lines. Split by named step, not by arbitrary length. | Ruff `PLR0915`; review |
| R2 | Cyclomatic complexity MUST be 10 or lower. | Ruff `C901` |
| R3 | A function MUST take at most 5 parameters and have at most 6 return points. Group related values in a named, immutable type. | Ruff `PLR0913`, `PLR0911` |
| R4 | Nesting SHOULD NOT exceed three levels. Use early returns and small helpers. | Review |
| R5 | A module SHOULD stay under about 400 lines and MUST have one reason to change. Split by responsibility, never into `utils.py`. | Review |
| R6 | Entry points (`main`, request handlers, CLI commands) MUST only parse input and call named steps. They contain no business logic. | Review |
| R7 | Nested functions and closures are allowed only to capture a local value. Shared logic belongs in a module-level function or class. | Review |
| R8 | Every public module, class and function MUST have a docstring stating its purpose and its non-obvious `Args`, `Returns` and `Raises`. Tests are exempt. | Ruff `D` (Google convention) |
| R9 | Comments MUST explain why, a constraint or evidence. They MUST NOT describe what the code does. Commented-out code MUST be deleted. | Ruff `ERA001`; review |
| R10 | Names MUST be full, domain-specific words. Avoid abbreviations and generic names such as `data`, `info`, `manager` or `helper`. Follow PEP 8. | Ruff `N`; review |
| R11 | Literal numbers and strings with meaning MUST be named constants. | Ruff `PLR2004` |
| R12 | Catch-all `except`, `assert` used for validation, and mutable default arguments MUST NOT be used. | Ruff `BLE001`, `S101`, `B006` |
| R13 | Each security invariant (audience check, scope check, tenant check, redaction, schema hash) MUST live in one named place with a docstring and a test that fails if it is removed. | Review; tests |
| R14 | A layer MAY import only from the layers below it. See [Import boundaries](#32-import-boundaries). | `import-linter` |
| R15 | A test MUST be named for the behavior it checks, check one behavior, and contain no conditional logic. | Review |
| R16 | A refactor and a behavior change MUST NOT share a change. A refactor MUST NOT alter a tool contract or schema hash. | Review; schema gate |

### 3.2 Import boundaries

The package is a stack of layers, enforced by `lint-imports` (contracts in `pyproject.toml`). A module may
import only from layers beneath it; modules on the same line are independent of each other.

1. `entrypoint`
2. `app`
3. `server` (transport and tool registration)
4. `policy`, `auth` (authorization and identity)
5. `worker` (isolated execution)
6. `tool_adapter`
7. `tool_registry`
8. `tool_models`
9. `settings`, `errors` (shared foundations)

`audit` and `telemetry` are independent of each other. Nothing in `src/` imports from `tools/`, and
nothing imports a private `stram_data` module. A new layer or an upward import needs an ADR in `PLANS.md`.

### 3.3 Baseline and ratchet

- The lint rules above are enabled repository-wide. Files that predate them list the rules they still
  violate in `[tool.ruff.lint.per-file-ignores]` under a `# legacy baseline` comment.
- A change MAY remove entries from the baseline. A change MUST NOT add an entry or touch a legacy
  function beyond what its purpose requires. If you edit a legacy function, bring that function within
  the rules.
- New files MUST NOT appear in the baseline.

### 3.4 Framework rules

- The server framework is FastMCP, pinned to one exact reviewed release. Application code uses the
  framework's public API only, never its private modules.
- Tool definitions are declared with the framework's decorators, one tool per small function that
  delegates to the existing adapter. A tool function validates nothing by hand that its input model
  already validates.
- Cross-cutting behavior (authentication, scope checks, audit, rate limiting, redaction) is added as
  framework middleware or authorization checks, not copied into tool functions.
- The framework version MUST NOT change without running the full contract and security suites and the
  tool-schema gate.
- The previous SDK server was removed with its selection switch. Its last working state is the
  `archive/sdk-server` branch; to roll back, deploy an image built from that branch.

## 4. MCP tool contract rules

- One tool MUST perform one clearly described read-only operation and map to named public Data
  Interface methods. Generic query, SQL, expression, URL-fetch, file-export, and execute tools are
  prohibited.
- Input and output schemas MUST be closed, versioned, deterministic, and generated or checked against
  the implementing types. Unknown properties MUST be rejected.
- Every string, array, numeric range, date span, row count, request body, and response body MUST have a
  justified bound. Wrapper limits MUST never exceed Data Interface limits.
- Tool descriptions MUST be factual and concise. They MUST NOT contain hidden instructions, ask the
  model to bypass policy, reference unrelated tools, or claim that annotations enforce security.
- Every tool MUST declare truthful read-only annotations, but server-side policy remains authoritative.
- Tool output MUST use structured content and a declared output schema. Upstream data is untrusted data,
  not instructions. The wrapper MUST preserve units, precision, stable IDs, lineage, revision,
  truncation, `as_of`, availability mode, evidence, warnings, and limitations.
- The service MUST NOT “helpfully” infer missing financial intent, widen dates, change availability,
  clamp an invalid value silently, fill data, retry a query, or substitute a different series.
- Schema changes require a generated diff and review. Breaking tool changes require a major service
  version or a new tool name; additive changes still require compatibility tests and release notes.
- Reviewed tool definitions, including annotations and input/output schemas, MUST have reproducible
  hashes checked in CI.

## 5. Security and privacy rules

- Treat every request, token claim, tool argument, lake string, proxy header, and dependency artifact as
  untrusted until validated at its boundary.
- Authentication MUST validate signature, allowlisted algorithm, issuer, audience/resource, expiry,
  not-before, and required claims on every protected request. Tokens MUST never be forwarded to the
  Data Plane or telemetry systems.
- Authorization MUST be per tool and per invocation. Tenant admission MUST be derived from validated
  claims and deployment policy, never a caller-controlled argument.
- Secrets MUST come from the approved runtime secret mechanism, never source, image layers, command-line
  arguments, test snapshots, logs, traces, metrics, exceptions, or `.env` committed to Git.
- Sensitive comparisons SHOULD use established library behavior; custom cryptography is prohibited.
  HMAC/signing code MUST use standard-library or reviewed cryptographic primitives and support key
  rotation.
- Host, Origin, proxy, and forwarded-header trust MUST use explicit allowlists. Production wildcard
  origins, hosts, audiences, issuers, scopes, tenants, or algorithms are prohibited.
- Error responses MUST reveal only a stable public code, retryability, and safe message. Filesystem
  paths, stack traces, SQL/DuckDB details, dependency versions, token claims, and internal topology are
  private.
- Tool arguments and results MUST NOT be recorded in normal logs or spans. Audit data stores only the
  normalized minimum access scope needed for accountability.
- The process MUST run with least privilege: non-root, dropped capabilities, no privilege escalation,
  read-only root and lake, bounded tmpfs, and the narrowest egress. The worker has no network.
- Security-relevant defaults and policy changes require explicit review, tests for denial paths, a
  policy-version change, and release notes.
- Dependency, image, SBOM, provenance, secret, and vulnerability checks MUST run in CI. Unreviewed
  high/critical findings block release; exceptions require owner, rationale, mitigation, and expiry.

## 6. Data correctness rules

- The exact `stram-data` release and Data Interface contract hash MUST be pinned and checked at build
  and startup. Contract or version mismatch fails readiness.
- `strict`, `estimated`, and `ex_post` MUST retain their upstream meanings. `ex_post` is always labelled
  hindsight-only. `estimated` evidence and uncovered periods MUST remain visible.
- Dates, timestamps, units, decimal precision, quality flags, stable event/record IDs, input IDs,
  retractions, and availability notes MUST pass through without semantic rewriting.
- `truncated=true` MUST remain visible. The service MUST NOT describe a truncated result as complete or
  invent pagination the interface does not provide.
- Missing, unknown, zero, false, unavailable, and undetermined are distinct states and MUST NOT be
  collapsed.
- Test fixtures MUST include revisions, late ingestion, missing values, retractions, exact decimals,
  estimated/uncovered availability, ambiguous series, truncation, and empty results.
- Any transformation added only for transport reasons MUST have round-trip/golden tests showing that
  the quantitative meaning is unchanged.

## 7. Reliability and resource rules

- Every external wait and tool call MUST have a deadline. Queue wait, execution, graceful drain, JWKS
  refresh, telemetry export, and shutdown budgets are configured separately.
- Retrying is allowed only for explicitly classified transient control-plane operations, with bounded
  attempts, exponential backoff, jitter, and observability. Data queries, authorization decisions, and
  audit appends MUST NOT be retried automatically.
- Backpressure is required. When queue, concurrency, memory, CPU, or output budgets are exhausted, the
  service rejects work with a safe overload response instead of accepting unbounded work.
- Worker timeout/crash MUST be isolated from the HTTP process, audited, and followed by bounded worker
  replacement. Repeated failure makes readiness false; it MUST NOT create a restart loop without delay.
- Startup MUST validate configuration, exact contract, read-only lake, audit chain/writer, worker, and
  initial verification keys before readiness. Liveness MUST remain shallow and side-effect free.
- Shutdown MUST stop admission, drain within a bound, complete durable audit outcomes, flush telemetry
  best-effort within its own bound, and terminate workers. It MUST eventually exit.
- Caches, if later justified, MUST define key, scope, size, TTL, invalidation, sensitive-data behavior,
  and correctness tests. No cache is added merely for anticipated performance.
- Performance changes require representative benchmarks and before/after evidence. Never trade away
  authorization, audit durability, correctness fields, or bounds for latency.

## 8. Audit and observability rules

- Audit, application logs, traces, and metrics are different evidence layers. They MUST NOT be treated
  as interchangeable or sent to the same retention/access policy by accident.
- Exactly one terminal audit outcome MUST be durable for every attempted tool invocation. Audit failure
  fails the invocation closed. The hash chain MUST be verified at startup and by an operator command.
- Audit records MUST be append-only, canonically encoded, sequence-numbered, tamper-evident, and linked
  to policy/tool/release versions. Key rotation and chain/checkpoint boundaries MUST be explicit.
- Application logs MUST be structured and correlated with trace/span/request IDs. Log fields MUST be
  encoded to prevent log injection and capped to prevent amplification.
- Traces MUST follow W3C context propagation and OpenTelemetry conventions. Incoming baggage MUST NOT
  become trusted authorization context or uncontrolled telemetry attributes.
- Metric names, units, types, and label sets MUST be documented. Labels MUST be low-cardinality; caller,
  tenant, request, series, indicator, event, and error message are prohibited metric labels.
- Redaction is allowlist-based at serialization/export boundaries and tested with seeded canary secrets.
  “Do not log this field” comments alone are not a control.
- Telemetry exporter failure SHOULD degrade observability without blocking safe reads; audit durability
  failure MUST block them. Both conditions require visible health/metrics and bounded buffering.

## 9. Testing principles

- Tests MUST demonstrate behavior and invariants, not mirror private implementation structure.
- Each change starts with or includes a failing test where practical. Bug fixes require a regression
  test that fails for the original defect.
- The test portfolio MUST include unit, public-contract, tool-schema, MCP client integration, security,
  read-only, failure-injection, worker lifecycle, audit-chain, redaction, telemetry, container, and
  rollback/smoke coverage appropriate to the milestone.
- Tests MUST be deterministic, hermetic by default, and independent of production credentials, network,
  wall clock, host timezone, execution order, or the real SIDDU lake. Use injected clocks and synthetic
  lakes. Live/integration tests require an explicit marker and isolated configuration.
- Tests MUST NOT use arbitrary sleeps for synchronization. Use events, bounded polling, fake clocks, or
  observable state transitions.
- Failure paths deserve first-class tests: deny, malformed, boundary values, timeout, cancellation,
  overload, worker crash, dependency outage, partial shutdown, audit failure, and redaction.
- Security-critical modules (authentication, authorization, tenant policy, audit integrity, safe error
  mapping, redaction) require complete branch coverage of defined decisions. The repository coverage
  threshold set in M0 MUST NOT decrease without an approved rationale.
- Property/fuzz tests SHOULD cover schema parsers and boundary combinatorics where they provide more
  confidence than hand-picked examples. Fuzzing MUST remain bounded and reproducible in CI.
- A test is not allowed to pass by weakening an assertion, deleting a scenario, broadening a mock, or
  changing a frozen expected contract unless the intended behavior change is reviewed and documented.
- Flaky tests are defects. Quarantine requires an owner, issue, expiry, and preserved coverage; silently
  rerunning until green is prohibited.

## 10. Dependencies and supply chain

- Prefer the standard library and existing dependencies. Every new dependency needs a documented use,
  maintenance/health check, license review, vulnerability review, and comparison with a simpler option.
- Direct dependencies MUST use compatible upper bounds where appropriate and resolve to exact versions
  in `uv.lock`. Git dependencies MUST pin an immutable tag/commit and verified repository.
- Dependency updates are isolated changes and run the full contract/security suite. Major updates need
  migration notes and rollback consideration.
- Builds MUST not download mutable code outside the lock/resolution process. Base images and release
  images MUST be referenced by digest in deployment artifacts.
- Generated artifacts MUST be reproducible or carry provenance explaining their inputs. Do not hand-edit
  generated schemas, lockfiles, SBOMs, or contract snapshots.

## 11. Change, review, and documentation discipline

- Before editing, inspect the relevant code, public contracts, tests, decision records, and current
  worktree. Never overwrite unrelated changes.
- A change MUST state its problem, scope, non-goals, risks, security/data impact, verification evidence,
  and rollback. Prefer one reviewable concern over a large mixed change.
- Architecture/security decisions that alter boundaries, trust, persistence, availability semantics,
  audit, deployment, or public contracts require a short ADR or a recorded decision in `PLANS.md`.
- Public behavior, configuration, runbooks, tool schemas, examples, and release notes MUST change in the
  same milestone as the code they describe.
- Reviews MUST examine the diff, not only green CI. Reviewers check boundary adherence, denial paths,
  data semantics, resource bounds, telemetry leakage, rollback, and whether a dependency/abstraction is
  necessary.
- Security-sensitive changes require explicit security review before release. No code-author approval
  substitutes for production authorization.
- Commits and pushes remain separately authorized. When authorized, history SHOULD be understandable,
  with no generated noise, secrets, unrelated formatting churn, or disabled checks.
- Temporary compatibility code and feature flags require an owner, removal condition, and expiry.
- Documentation MUST follow the [Diátaxis](https://diataxis.fr/) structure in `docs/`. Each page has one
  purpose and one type: a **tutorial** teaches a beginner by guided doing, a **how-to guide** helps a
  competent reader finish one task, **reference** neutrally describes facts, and **explanation**
  discusses why. Do not mix types on one page; link instead of repeating.
- Tutorials MUST work every time and show expected output. How-to titles say what they achieve ("How to
  ..."). Reference mirrors the code and contains no instructions. Explanation titles start "About ...".
- `docs/README.md` MUST link every page. Documentation commands, links and examples are checked by
  `tools/check_docs.py` and MUST pass.

## 12. CI quality gates

The merge/release pipeline MUST fail on:

- formatting, lint, strict typing, or test failure;
- public contract or reviewed tool-schema drift without an approved update;
- internal `stram_data` imports, SQL/query construction, write-capable tool behavior, or forbidden APIs;
- insufficient security-critical branch coverage or a reduced repository threshold;
- dependency lock drift, prohibited license, committed secret, or unreviewed high/critical finding;
- seeded secret/path leakage in errors, logs, traces, metrics, audit, or tool output;
- container running as root, writable lake/root filesystem, excess capabilities, or unexpected egress;
- missing SBOM/provenance/schema hashes/test evidence for a release artifact.
- a readability-rule or import-boundary violation outside the recorded legacy baseline, or a baseline that grew;
- a documentation page missing from `docs/README.md` or a broken documentation link or example.

CI permissions MUST be read-only by default, third-party actions MUST be pinned to reviewed immutable
revisions, and untrusted pull-request code MUST not receive release credentials.

## 13. Definition of done

A change is done only when all applicable statements are true:

1. Scope and non-goals match the approved milestone; boundaries remain intact.
2. Code is typed, small, readable, documented where necessary, and contains no dead or speculative code.
3. Success, boundary, denial, and failure behavior have deterministic tests.
4. Security, privacy, data-correctness, audit, resource, and rollback impacts were reviewed.
5. Ruff, strict mypy, tests, contract/schema checks, scans, and artifact checks pass from the frozen lock.
6. Documentation, decisions, configuration schema, runbooks, and release notes are current.
7. The diff contains no secrets, unrelated edits, weakened checks, unbounded work, or internal Data Plane
   coupling.
8. Test commands and results, limitations, deferred risks, and anything not verified are recorded.
9. No deployment, credential access, production mutation, commit, or push occurred without its separate
   authorization.

## 14. Exceptions

An exception is rare and temporary. It MUST be recorded before merge with the exact rule, reason,
scope, owner, risk, compensating control, verification, expiry/removal trigger, and approving reviewer.
Exceptions MUST NOT weaken the read-only boundary, permit arbitrary execution/SQL, bypass authentication
or per-tool authorization, hide availability semantics, expose secrets, or make audit optional.
