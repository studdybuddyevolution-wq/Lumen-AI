# Lumen self-development and test sandbox

Lumen's self-development workflow must never test a proposed change in the live
checkout and then immediately load it.

## Current sandbox foundation

backtalk.selfdev.sandbox.SandboxRunner creates a disposable snapshot of a
project and runs a fixed set of checks in a separate Python process. Supported
checks are:

- compileall — syntax/bytecode compilation of the snapshot.
- unittest — Python's standard-library test discovery.
- pytest — optional pytest test discovery when requested.

The runner excludes Git metadata, virtual environments, caches, .env files,
personal backtalk.json, symlinks, and existing feature approval/verification
sidecars. It enforces snapshot size/file-count limits, has per-check timeouts,
caps captured output, removes common credential-bearing environment variables,
and returns SHA-256 digests plus a verification receipt.

The live source directory is not used as the test working directory. Tests may
write files inside the temporary snapshot; that directory is removed after the
run. A receipt passes only if every requested check passes and the original
source tree's digest remains unchanged.

## Critical security boundary

This first implementation is workspace-copy and subprocess isolation, not a
security sandbox. It does not use a restricted Windows token, AppContainer,
Windows Sandbox, Hyper-V, or a container runtime. Code executed by the test
process can still access files and network resources available to the current
Windows account. Removing proxy variables is not equivalent to blocking
network access.

Therefore:
- Use this runner for regression tests of Lumen's own checked-out code.
- Do not use it to execute hostile third-party code or arbitrary binaries.
- Never describe a passing receipt as proof that generated code is safe.
- A hardened Windows Sandbox/AppContainer or container backend is required
  before Lumen executes untrusted generated code with security guarantees.

## Feature activation gate

Dynamic feature modules now require both:
1. A passing verification receipt matching the exact current Python file bytes.
2. A separate explicit human approval matching those same bytes.

The intended sequence is:

1. Generate or modify a feature in an isolated Git worktree.
2. Run the candidate project's tests using the sandbox runner.
3. Record the receipt with record_feature_verification(feature_path, receipt).
4. Review the diff and explicitly approve with approve_feature_module(feature_path).
5. Only then may the registry load the module.

Changing the feature after verification invalidates its receipt. Changing it after
approval invalidates its approval hash too. A test pass never grants approval.

This is a foundation for the full Bug Hunter / Skill Forge / Auto-Heal cycle;
it does not yet run autonomous self-development or expose a dashboard control.


## DGM-inspired evolution archive (design requirement)

The [Darwin Gödel Machine (DGM)](https://github.com/jennyzzt/dgm) demonstrates
an important pattern for iterative agent improvement: preserve an archive of
candidate agents, select a parent candidate, propose one general improvement
from its evaluation logs, create a child candidate, and evaluate that candidate.
Do not reduce the process to "always modify the latest version" or "always
modify the current highest scorer". A lower-scoring but viable candidate may
contain a useful stepping stone for a later improvement.

DGM's repository declares Apache-2.0. Any future direct source reuse must retain
the required license/copyright notices and inspect third-party components
separately. Its SWE-bench and Polyglot evaluation code/data are separate
projects and are not automatically covered by DGM's top-level license.

### Lumen adaptation

When the self-development controller is implemented, maintain an append-only
candidate archive. Each candidate record should include:

- Candidate ID, parent candidate ID, source commit, branch/worktree and creation time.
- A concise description of the single improvement attempted.
- Changed-file list and source SHA-256 digest.
- Sandbox receipt ID, check names, outcomes and test environment metadata.
- Capability-evaluation suite version, per-task results and aggregate score.
- Safety-gate results, review status, user decision and promotion/retirement reason.
- Resource use, elapsed time and model/provider used (local Qwen by default).

Keep candidate source snapshots and evaluation records immutable once recorded.
A failed candidate remains in experiment history but never becomes loadable or
active. Keep viable alternatives available for future experiments. Only a
separately approved, verified candidate may be promoted into Lumen's active
source tree.

### Bounded evolutionary loop

1. Gather recurring failures, bug reports, tool-selection misses, user-approved
   feedback, and regression results.
2. Diagnose the evidence and propose one general capability improvement.
3. Select a parent from the archive using a bounded strategy that balances
   measured quality with exploration of less-used lineages.
4. Create a separate Git worktree and generate the candidate change there.
5. Run static checks, regression tests and disposable sandbox checks.
6. Run a fixed capability benchmark. Start with a small smoke subset; run a
   larger held-out evaluation only for promising candidates.
7. Record results and lineage. Keep inferior attempts as history, but promote
   only candidates that pass all safety gates and show repeatable improvement
   without unacceptable regressions.
8. Require explicit human approval before integrating source changes or
   activating a generated feature.

Early Lumen measurements should cover tool-routing accuracy, schema-valid tool
calls, successful task completion, correct file-edit/backup behavior, memory
retrieval quality, latency and regression rates. Voice metrics can be evaluated
separately. Never use a single aggregate score as a substitute for safety
requirements.

### Anti-gaming and fixed governance

The candidate agent must not rewrite the evaluator, test fixtures, baseline
results, safety policy, approval mechanism, rollback logic or evolution
controller. Keep these governance components outside the candidate's writable
workspace and verify them against a trusted source before each experiment.

Use held-out tasks and regression cases to reduce benchmark overfitting. Include
explicit tests for policy bypass, secret exposure, unsafe file access and
unauthorized feature activation. A candidate that improves a benchmark while
weakening a safety gate is rejected regardless of its score.

Run only a small number of experiments concurrently on Lumen's local hardware.
The default should be one candidate at a time, with strict limits on iterations,
runtime, snapshot size and resource consumption. Local Qwen/Ollama remains the
default provider; any optional cloud code assistant must follow Lumen's privacy
and redaction rules.

### What not to port from DGM

DGM is a research implementation, not a drop-in Windows self-hosting service.
Its entry points use Linux shell tools and Docker workflows, and its experiment
stack depends on external model APIs and benchmark infrastructure.

Do not copy its general-purpose shell tool, unrestricted file editor, reset
routine, Docker invocation or container archive extraction helpers into Lumen.
In particular, its Git reset helper uses hard reset and clean operations that
can remove untracked files. Its Docker helper does not establish the resource,
network, filesystem and user restrictions Lumen requires for hostile-code
execution. Rebuild the useful workflow around Lumen's Windows-aware Git
worktrees, sandbox backend, explicit approvals and rollback policy.

DGM's research results demonstrate the value of archive-based exploration for
coding-agent benchmarks, not guaranteed gains for a local desktop assistant.
Lumen must evaluate its own real tasks.


## Phase 4: candidate archive and evaluation controller

The first archive implementation is available in
`backtalk.selfdev.archive`.

### Candidate and evaluation records

`CandidateArchive` stores one JSON record per candidate, evaluation, and
review decision under the selected archive directory:

- `candidates/`: immutable candidate manifest containing candidate ID,
  parent ID, source branch/commit, filtered source-tree SHA-256, changed-file
  list, one-improvement summary, suite version and creation time.
- `evaluations/`: immutable evaluation result containing sandbox receipt IDs,
  per-check pass/fail summaries, benchmark metrics, safety-gate results,
  environment metadata, duration and failure stage.
- `decisions/`: immutable retain/reject/promote decisions and their rationale.
  A later decision appends a new record instead of rewriting earlier history.

The archive directory must be outside the candidate source tree. The API does
not store an absolute worktree path or raw test stdout/stderr. The archive is
append-only through this Python API, but ordinary local filesystem access can
still alter or delete its files; it is not tamper-proof storage.

Candidate source code stays in its separate Git branch/worktree. This phase
records the branch and commit metadata but does not yet create or manage Git
worktrees. It also does not generate candidate patches or activate features.

### Staged evaluation

`CandidateEvaluationController.evaluate(...)` first checks that the current
filtered source-tree digest still matches the candidate manifest. It then:

1. Runs a `compileall` smoke check against a disposable project copy.
2. Only when that passes, runs `compileall` plus the complete standard-library
   `unittest` discovery suite against another disposable copy.
3. Re-checks the source digest and records a failed evaluation if the source
   changed during the process.
4. Records compact check summaries and receipt IDs, not test logs.

To pass evaluation, both stages must pass, benchmark metrics must be supplied,
and the safety result must report `security_policy_passed=true`,
`policy_regressions=0`, and all named safety checks passing.

The current required benchmark dimensions are kept separate:
`task_success_rate`, `tool_routing_accuracy`, `schema_validity_rate`, and
`median_latency_ms`. Additional numeric metrics can be recorded. There is no
capability benchmark harness in this phase: a trusted evaluator must supply
these values. The controller deliberately does not invent scores or treat
passing unit tests as evidence of improved task performance.

### Parent selection and promotion

`CandidateArchive.select_parent(seed=...)` considers only candidates whose
latest decision is `retain` or `promote`, whose latest evaluation passed,
and whose safety results pass. It computes a Pareto front across the four
separate capability dimensions, then gives less-used lineages a higher chance
of selection. Safety results are hard eligibility gates, never a score that
can be traded against capability gains.

A candidate can be retained only after a passing evaluation. A `promote`
decision requires `human_approved=True`; it is still only an archive decision
and does not install code, load a feature module, or change the active checkout.
The existing verification receipt and explicit feature-approval gates remain
mandatory for dynamic feature activation.

### Current limits

- The copy-based runner is not an OS security boundary. It cannot safely run
  hostile or untrusted generated code with the current Windows user's access.
- The archive does not yet create worktrees, run model-generated patches,
  execute a real Lumen capability benchmark, or perform deployment/rollback.
- Metrics and safety outcomes are only as trustworthy as the evaluator that
  supplies them. Keep that evaluator, its test fixtures, policy checks and
  approval logic outside candidate-writable source.
- Run one local experiment at a time by default. Do not allow candidates to
  rewrite the archive controller, test suites, safety gates, approval logic or
  rollback rules.


## Phase 5: Bug Hunter diagnostics

The first Bug Hunter implementation is available in
`backtalk.selfdev.bug_hunter`.

### Runtime capture

A `BugHunter` instance is attached to `WarmBrain`. It captures unexpected
exceptions at these boundaries:

- Tool handler failures after schema validation.
- Ollama startup failures.
- Ollama/chat-turn failures.
- Unexpected localhost bridge chat and request-handler failures.

Expected tool denials, invalid arguments, and user cancellation are not treated
as bugs. Capture is best-effort: a diagnostic failure must never change the
original action's result or take the voice loop down.

### Traceback analysis

The Bug Hunter parses formatted Python traceback frames, keeps only frames that
resolve inside the Backtalk project root, and selects the innermost in-project
frame as the primary location. It extracts a small source excerpt and uses the
Python AST to identify the containing function or class when the file parses.
Syntax-error files still retain a bounded line excerpt even though no AST
symbol can be identified.

Each report includes a stable fingerprint, exception type, sanitised message,
component label, project-relative frame locations, AST symbol, small source
context, first/last-seen timestamps and recurrence count. Repeated equivalent
failures increment one report instead of creating unbounded duplicates.

### Privacy and resource limits

- Reports stay in memory and are capped at 100 unique fingerprints per runtime.
- Raw tracebacks, local variables, tool arguments, conversation history and
  full absolute paths are not collected as separate fields.
- Exception messages or source lines can themselves embed user-provided text;
  these are subject to pattern-based redaction, so reports must still be
  treated as potentially sensitive. Do not deliberately place private inputs
  or credentials in exception messages or source code.
- Messages and source excerpts are bounded and common API keys, passwords,
  bearer tokens, private-key blocks, token-like values and Windows user paths
  are redacted before records are returned.
- Only source files inside the configured project root are inspected; resolved
  paths are checked for project containment and symlink traversal.
- The localhost bridge exposes read-only summaries at
  `GET /bridge/bugs?limit=50`. Reports are newest-first and the limit is capped
  at 100. The HTTP response includes exception summaries, file/function/line
  locations and AST symbols, but omits source excerpts and source-line text.

### Current limits

- Reports are process-local and disappear when Backtalk exits; persistent
  cross-session diagnostics are not implemented in this phase.
- This is reactive exception capture, not a background filesystem watcher,
  static linter, crash-dump collector or Windows Event Log monitor.
- Fingerprints group similar errors; they do not prove a root cause. The AST
  symbol and source excerpt are diagnostic clues, not an automatic diagnosis.
- The Bug Hunter never edits source code, generates patches, runs a repair
  agent, or activates features. Those tasks remain subject to isolated
  candidate worktrees, staged evaluation, explicit human review and approval.
- Redaction uses conservative pattern matching and cannot guarantee detection
  of every secret format. Do not deliberately place credentials in exception
  messages or source files.


## Phase 6: Skill Forge

The first Skill Forge implementation is available in
`backtalk.selfdev.skill_forge`. It prepares proposed pure-Python tools in
dedicated Git worktrees and returns source-bound review packets. It does not
install or activate generated modules.

### Candidate lifecycle

1. Construct a `SkillSpecification` with a unique candidate ID, tool name,
   purpose, entry point, input/output JSON Schemas, requested permissions,
   optional filesystem scope, network/dependency requirements, expected
   behavior, failure modes and proposed tests.
2. `OllamaSkillGenerator` sends only that specification to an HTTP loopback
   Ollama endpoint. It defaults to `qwen3.5:4b`, disables environment proxy
   inheritance, and does not receive conversation history, vault contents,
   credentials or unrelated files. Cloud generation is not implemented.
3. The generator returns exactly one source file and one unittest file.
   Neither is imported or executed by Skill Forge.
4. `GitWorktreeManager` creates a branch named
   `skillforge/<candidate-id>` and a separate worktree outside the main
   checkout. By default the worktree root is a sibling directory named
   `.<repository-name>-skill-worktrees`. Creation requires the main checkout
   to be clean, including untracked files. Candidate files are created with
   exclusive writes; existing branches/files are not overwritten.
5. `StaticSkillValidator` parses Python ASTs, compiles syntax to code objects
   without executing them, checks imports, entry-point registration, literal
   schema/description, non-mutating registration, module/class side effects,
   function defaults and annotations, and denies permissions, direct
   filesystem scope, network access and third-party dependencies.
6. The candidate source digest, changed-file list, base commit, branch,
   candidate-relative worktree reference, specification, generator identifier
   and static findings are appended to `CandidateArchive` metadata. The
   archive's metadata field is optional so Phase 4 records remain readable.
7. `SkillForge.review_candidate(candidate_id)` returns a
   `SkillReviewPacket` containing the exact source and test files, source
   digest, requested permissions, static findings, runtime status and current
   review status. It refuses to return a packet when the candidate tree has
   drifted from its archived digest.
8. `record_human_review(...)` appends a digest-bound `approved` or
   `rejected` review. A rejected candidate cannot be approved through stale
   source evidence.
9. `approve_installation(...)` can record a separate installation-approval
   record only after a current approved review, explicit human confirmation,
   exact source digest and a recorded passing receipt from a restricted
   execution backend. This record is metadata only: no installer or runtime
   loader is called.

### Example: prepare a local candidate

Run from the Backtalk project directory after installing the project's
existing dependencies and starting local Ollama. Keep the evolution archive
outside the repository so it does not dirty the main checkout.

```python
from pathlib import Path

from backtalk.selfdev.archive import CandidateArchive
from backtalk.selfdev.skill_forge import SkillForge, SkillSpecification

repository_root = Path.cwd().parent
archive = CandidateArchive(Path.home() / ".lumen" / "evolution-archive")
forge = SkillForge(repository_root, archive)

specification = SkillSpecification(
    name="uppercase_text",
    description="Convert supplied text to uppercase.",
    intended_use="Provide a deterministic text transformation.",
    entry_point="backtalk.features.uppercase_text:run",
    input_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    output_schema={"type": "string"},
    expected_behavior=("Return the supplied text in uppercase.",),
    proposed_tests=("Uppercase ASCII text.", "Handle empty text."),
)

candidate = forge.prepare_candidate(specification)
packet = forge.review_candidate(candidate.candidate_id)
print(packet.candidate_id, packet.static_validation["passed"])
print(packet.runtime_status, packet.installation_status, packet.activation_status)
```

The main checkout must be clean before `prepare_candidate` creates the
worktree. The source and generated tests are placed on the candidate branch;
the local model response is never imported or executed. Review the returned
`packet.source_files` and manifest before recording a human review.

### Execution isolation: intentionally not available yet

The default execution backend is `UnavailableExecutionBackend`. Calling
`evaluate_candidate` returns `awaiting_isolated_execution` and does not start
a subprocess or execute candidate code. A restricted backend interface exists
for a future Windows Sandbox/AppContainer implementation, but no concrete
secure Windows execution backend is shipped in Phase 6.

Do not substitute the Phase 3 `SandboxRunner` for this backend. That runner
copies the project and starts a subprocess, but the subprocess still has the
current Windows user's filesystem and network permissions. A Git worktree is
also only source separation. Neither mechanism is a hardened OS security
boundary. Static validation is a conservative policy screen, not a proof that
generated code is safe.

### Permissions and installation boundaries

Phase 6 grants no generated-tool permissions. Requests for permissions,
filesystem access, network access or third-party dependencies cause static
validation to fail and remain visible in the review packet. Generated tools
must register as non-mutating functions with literal metadata matching their
specification. The generated test file is supplied for human review but is
not run by the local copy-based sandbox.

Human code review, runtime execution approval, installation and activation
are separate stages. The current implementation supports review records and
records a conditional installation approval, but it does not implement an
installer, approve a feature sidecar, load a module, activate code, or wire
Skill Forge into the chat/bridge/dashboard. In particular, an archive
`promote` decision is not runtime approval.

### Git and recovery

- Candidate worktrees and branches are retained for inspection.
- `GitWorktreeManager.abandon(...)` refuses to remove a worktree with tracked
  changes, staged changes or untracked files. When it does remove a clean
  worktree, it deliberately retains the candidate branch for audit/recovery.
- Do not use `git clean`, `git reset --hard`, or recursive deletion to
  recover failed candidate creation. Inspect partially-created worktrees and
  resolve them deliberately.
- The candidate-relative worktree reference is stored in the archive rather
  than an absolute machine path. The worktree root is resolved by the local
  Skill Forge instance.

### Verification

Run the unit tests from the Backtalk project directory in Windows PowerShell:

```powershell
cd C:\Users\ARIN\Lumen\backtalk
uv run python -m unittest discover -s tests -v
```

The 30 Phase 6 test methods cover specification validation, AST-only static checks,
prohibited operations and permissions, loopback-only Ollama configuration,
worktree creation and refusal behavior, archive metadata, source-drift
rejection, fail-closed execution, review, and installation-approval gates.

### Current limits

- There is no concrete restricted Windows execution backend, real runtime
  evaluation receipt, installer or activation workflow yet.
- The review packet is a Python API result; this phase does not add a dashboard
  or bridge review screen.
- No generated candidate is added to the live feature directory or loaded into
  the running registry.
- Static AST rules can miss behaviors they do not recognize. They must never
  be treated as the OS isolation boundary or as a substitute for human review.
- The archive is append-only through its API, not tamper-proof against direct
  local filesystem modification.
- The model may return malformed, incomplete or poor-quality code. Such output
  can be rejected during validation or human review; generation is not a
  guarantee of correctness.
