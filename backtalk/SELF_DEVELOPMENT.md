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

The [Darwin Gödel Machine (DGM)](https://github.com/jennyzzt/dgm) demonstrates a
useful pattern for iterative agent improvement: keep an archive of candidate
agents, select a parent candidate, propose one general improvement from its
evaluation logs, create a child candidate, and evaluate that candidate. Do not
reduce the process to "always modify the latest version" or "always modify the
current highest scorer". A lower-scoring but viable candidate may contain a
useful stepping stone for a later improvement.

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
A failed candidate should remain in experiment history, but it must never become
loadable or active. Keep viable alternatives available for future experiments.
Only the separately approved, verified candidate may be promoted into Lumen's
active source tree.

### Bounded evolutionary loop

1. Gather recurring failures, bug reports, tool-selection misses, user-approved
   feedback, and regression results.
2. Diagnose the evidence and propose one general capability improvement.
3. Select a parent from the candidate archive using a bounded strategy that
   balances measured quality with exploration of less-used lineages.
4. Create a separate Git worktree and generate the candidate change there.
5. Run static checks, regression tests and the disposable sandbox checks.
6. Run a fixed capability benchmark. Start with a small smoke subset; run a
   larger held-out evaluation only for promising candidates.
7. Record results and lineage. Keep failed or inferior attempts as history,
   but promote only candidates that pass every safety gate and show a
   repeatable improvement without unacceptable regressions.
8. Require explicit human approval before integrating source changes or
   activating a generated feature.

For Lumen, early capability measurements should cover tool-routing accuracy,
schema-valid tool calls, successful task completion, correct file-edit/backup
behavior, memory retrieval quality, latency and regression rates. Voice metrics
can be evaluated separately. Never use a single aggregate score as a substitute
for safety requirements.

### Anti-gaming and fixed governance

The candidate agent must not be allowed to rewrite the evaluator, test fixtures,
baseline results, safety policy, approval mechanism, rollback logic or evolution
controller. Keep these governance components outside the candidate's writable
workspace and verify them against a trusted source before each experiment.

Use held-out tasks and regression cases to reduce benchmark overfitting. Include
explicit tests for policy bypass, secret exposure, unsafe file access and
unauthorized feature activation. A candidate that improves a benchmark while
weakening a safety gate is rejected regardless of its score.

Run only a small number of experiments concurrently on Lumen's local hardware.
The default should be one candidate at a time, with strict limits on iterations,
runtime, snapshot size and resource consumption. Local Qwen/Ollama remains the
default reasoning provider; any optional cloud code assistant must follow
Lumen's separate privacy and redaction rules.

### What not to port from DGM

DGM is a research implementation, not a drop-in Windows self-hosting service.
Its entry points use Linux shell tools and Docker workflows, and its experiment
stack depends on external model APIs and benchmark infrastructure.

Do not copy its general-purpose shell tool, unrestricted file editor, reset
routine, Docker invocation or container archive extraction helpers into Lumen.
In particular, its Git reset helper uses hard reset and clean operations that
can remove untracked files; its Docker helper does not establish the resource,
network, filesystem and user restrictions Lumen requires for hostile-code
execution. Rebuild the useful workflow around Lumen's Windows-aware Git
worktrees, disposable sandbox backend, explicit approvals and rollback policy.

DGM's research results are evidence for the value of archive-based exploration
in coding-agent benchmarks, not a promise that the same performance gains will
transfer to a local desktop assistant. Lumen must evaluate its own real tasks.
