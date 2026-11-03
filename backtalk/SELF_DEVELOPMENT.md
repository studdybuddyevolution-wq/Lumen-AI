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
