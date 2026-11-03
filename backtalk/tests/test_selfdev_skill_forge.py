from __future__ import annotations

import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from backtalk.selfdev.archive import CandidateArchive
from backtalk.selfdev.skill_forge import (
    ExecutionReceipt,
    GeneratedSkill,
    GitWorktreeManager,
    OllamaSkillGenerator,
    SkillForge,
    SkillForgeError,
    SkillGenerationError,
    SkillSpecification,
    StaticSkillValidator,
)
from backtalk.selfdev.sandbox import project_tree_digest


INPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA = {"type": "string"}


def make_spec(**kwargs) -> SkillSpecification:
    values = {
        "name": "echo_upper",
        "description": "Convert text to uppercase.",
        "intended_use": "A pure text transformation tool.",
        "entry_point": "backtalk.features.echo_upper:run",
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "proposed_tests": ("uppercase text", "empty input"),
    }
    values.update(kwargs)
    return SkillSpecification(**values)


def make_generated() -> GeneratedSkill:
    source = '''"""Pure text transformation."""
from backtalk.registry import tool

@tool(
    name="echo_upper",
    description="Convert text to uppercase.",
    params_schema={
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    },
    mutating=False,
)
def run(text: str) -> str:
    return text.upper()
'''
    tests = '''import unittest
from backtalk.features.echo_upper import run

class EchoUpperTests(unittest.TestCase):
    def test_uppercases_text(self):
        self.assertEqual(run("hello"), "HELLO")
'''
    return GeneratedSkill(source=source, tests=tests)


class StubGenerator:
    name = "test-generator"

    def generate(self, specification: SkillSpecification) -> GeneratedSkill:
        return make_generated()


class PassingExecutionBackend:
    available = True

    def run_candidate(self, worktree_path, *, candidate_id, source_sha256):
        return ExecutionReceipt(
            receipt_id=uuid.uuid4().hex,
            candidate_id=candidate_id,
            source_sha256=source_sha256,
            status="passed",
            passed=True,
            security_boundary=True,
            checks=("compileall", "unittest"),
            completed_at="2026-11-03T12:00:00+00:00",
        )


def init_git_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    (root / "backtalk" / "backtalk" / "features").mkdir(parents=True)
    (root / "backtalk" / "tests").mkdir(parents=True)
    (root / "backtalk" / "backtalk" / "features" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (root / "backtalk" / "tests" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (root / "README.md").write_text("Lumen test repository\n", encoding="utf-8")
    commands = [
        ["git", "init", "-b", "main"],
        ["git", "add", "-A"],
        [
            "git", "-c", "user.name=Skill Forge Tests",
            "-c", "user.email=skill-forge-tests@example.invalid",
            "commit", "-m", "test base",
        ],
    ]
    for command in commands:
        result = subprocess.run(
            command, cwd=root, capture_output=True, text=True, check=False
        )
        if result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return head


class SkillSpecificationTests(unittest.TestCase):
    def test_accepts_valid_specification_and_emits_json_compatible_values(self):
        spec = make_spec()
        spec.validate()
        payload = spec.to_dict()
        self.assertEqual(payload["name"], "echo_upper")
        self.assertIsInstance(payload["proposed_tests"], list)

    def test_rejects_invalid_name_entrypoint_and_json_schema(self):
        with self.assertRaises(SkillForgeError):
            make_spec(name="../escape").validate()
        with self.assertRaises(SkillForgeError):
            make_spec(entry_point="backtalk.features.other:run").validate()
        with self.assertRaises(SkillForgeError):
            make_spec(input_schema={"type": "not-a-schema"}).validate()

    def test_rejects_invalid_candidate_parent_identifier(self):
        with self.assertRaises(SkillForgeError):
            make_spec(parent_candidate_id="../main").validate()


class StaticSkillValidatorTests(unittest.TestCase):
    def test_accepts_pure_tool_and_unittest_source_without_running_it(self):
        spec = make_spec()
        generated = make_generated()
        result = StaticSkillValidator().validate(
            spec,
            {spec.source_path: generated.source, spec.test_path: generated.tests},
        )
        self.assertTrue(result.passed, result.findings)
        self.assertEqual(set(result.source_hashes), {spec.source_path, spec.test_path})

    def test_static_validation_never_executes_module_side_effects(self):
        spec = make_spec()
        marker = Path(tempfile.gettempdir()) / f"lumen-skillforge-{uuid.uuid4().hex}.marker"
        source = (
            "from backtalk.registry import tool\n"
            f"open({str(marker)!r}, 'w').write('should-not-run')\n"
            "@tool(name='echo_upper', description='x', params_schema="
            + repr(INPUT_SCHEMA)
            + ", mutating=False)\n"
            "def run(text: str) -> str:\n"
            "    return text\n"
        )
        result = StaticSkillValidator().validate(
            spec, {spec.source_path: source, spec.test_path: make_generated().tests}
        )
        self.assertFalse(result.passed)
        self.assertFalse(marker.exists())
        self.assertTrue(any(item.code in {"dangerous-call", "module-side-effect"} for item in result.findings))

    def test_rejects_prohibited_imports_and_dynamic_execution(self):
        spec = make_spec()
        source = (
            "import subprocess\n"
            "from backtalk.registry import tool\n"
            "@tool(name='echo_upper', description='x', params_schema="
            + repr(INPUT_SCHEMA)
            + ", mutating=False)\n"
            "def run(text: str) -> str:\n"
            "    return eval(text)\n"
        )
        result = StaticSkillValidator().validate(
            spec, {spec.source_path: source, spec.test_path: make_generated().tests}
        )
        codes = {item.code for item in result.findings}
        self.assertFalse(result.passed)
        self.assertIn("import-denied", codes)
        self.assertIn("dangerous-call", codes)

    def test_denies_requested_permissions_network_filesystem_and_dependencies(self):
        spec = make_spec(
            required_permissions=("workspace.read",),
            filesystem_scope=("workspace",),
            network_access_required=True,
            dependencies=("requests",),
        )
        result = StaticSkillValidator().validate(
            spec, {spec.source_path: make_generated().source, spec.test_path: make_generated().tests}
        )
        codes = {item.code for item in result.findings}
        self.assertFalse(result.passed)
        self.assertTrue({
            "permission-denied", "filesystem-denied", "network-denied", "dependencies-denied"
        }.issubset(codes))

    def test_rejects_wrong_registration_schema_or_mutating_tool(self):
        spec = make_spec()
        source = make_generated().source.replace("mutating=False", "mutating=True")
        result = StaticSkillValidator().validate(
            spec, {spec.source_path: source, spec.test_path: make_generated().tests}
        )
        self.assertFalse(result.passed)
        self.assertIn("mutating-tool", {item.code for item in result.findings})

    def test_rejects_entrypoint_signature_that_differs_from_schema(self):
        spec = make_spec()
        source = make_generated().source.replace("def run(text: str)", "def run(value: str)")
        result = StaticSkillValidator().validate(
            spec, {spec.source_path: source, spec.test_path: make_generated().tests}
        )
        self.assertFalse(result.passed)
        self.assertIn("tool-signature", {item.code for item in result.findings})

    def test_rejects_executable_function_defaults_and_annotations(self):
        spec = make_spec()
        source = make_generated().source.replace(
            "def run(text: str) -> str:",
            "def run(text: str = open('should-not-run')) -> str:",
        )
        result = StaticSkillValidator().validate(
            spec, {spec.source_path: source, spec.test_path: make_generated().tests}
        )
        codes = {item.code for item in result.findings}
        self.assertFalse(result.passed)
        self.assertIn("dangerous-call", codes)
        self.assertIn("function-default", codes)

    def test_rejects_path_traversal_and_extra_generated_files(self):
        spec = make_spec()
        with self.assertRaises(SkillForgeError):
            StaticSkillValidator().validate(
                spec,
                {
                    spec.source_path: make_generated().source,
                    spec.test_path: make_generated().tests,
                    "../escape.py": "pass",
                },
            )

    def test_rejects_syntax_errors(self):
        spec = make_spec()
        result = StaticSkillValidator().validate(
            spec,
            {spec.source_path: "def broken(:\n    pass\n", spec.test_path: make_generated().tests},
        )
        self.assertFalse(result.passed)
        self.assertIn("syntax-error", {item.code for item in result.findings})


class OllamaGeneratorTests(unittest.TestCase):
    def test_generator_rejects_non_loopback_model_endpoint(self):
        with self.assertRaises(SkillForgeError):
            OllamaSkillGenerator(base_url="https://example.com")
        with self.assertRaises(SkillForgeError):
            OllamaSkillGenerator(base_url="http://192.168.1.5:11434")

    def test_generator_is_configured_for_loopback_only(self):
        generator = OllamaSkillGenerator()
        self.assertEqual(generator.base_url, "http://127.0.0.1:11434")
        self.assertEqual(generator.name, "ollama-local")


class GitWorktreeManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.repo = self.parent / "repo"
        self.base_commit = init_git_repo(self.repo)
        self.manager = GitWorktreeManager(self.repo)

    def test_creates_candidate_branch_and_worktree_without_touching_main_checkout(self):
        candidate_id = uuid.uuid4().hex
        worktree = self.manager.create(candidate_id, base_commit=self.base_commit)
        self.assertTrue(Path(worktree.path).is_dir())
        self.assertEqual(worktree.base_commit, self.base_commit)
        self.assertFalse((self.repo / "candidate.py").exists())
        self.manager.write_file(worktree, "candidate.py", "VALUE = 42\n")
        self.assertTrue((Path(worktree.path) / "candidate.py").is_file())
        self.assertFalse((self.repo / "candidate.py").exists())

    def test_refuses_dirty_main_checkout(self):
        (self.repo / "unrelated-user-file.txt").write_text("keep me", encoding="utf-8")
        with self.assertRaisesRegex(SkillForgeError, "dirty"):
            self.manager.create(uuid.uuid4().hex)

    def test_refuses_existing_candidate_branch(self):
        candidate_id = uuid.uuid4().hex
        worktree = self.manager.create(candidate_id)
        with self.assertRaisesRegex(SkillForgeError, "branch already exists"):
            self.manager.create(candidate_id)
        self.assertTrue(Path(worktree.path).is_dir())

    def test_refuses_path_traversal_and_file_overwrite(self):
        worktree = self.manager.create(uuid.uuid4().hex)
        with self.assertRaises(SkillForgeError):
            self.manager.write_file(worktree, "../outside.py", "bad")
        with self.assertRaises(SkillForgeError):
            self.manager.write_file(worktree, "notes.txt:stream", "bad")
        self.manager.write_file(worktree, "candidate.py", "VALUE = 1\n")
        with self.assertRaisesRegex(SkillForgeError, "overwrite"):
            self.manager.write_file(worktree, "candidate.py", "VALUE = 2\n")

    def test_refuses_to_remove_a_dirty_worktree_and_removes_a_clean_one(self):
        worktree = self.manager.create(uuid.uuid4().hex)
        self.manager.write_file(worktree, "candidate.py", "VALUE = 1\n")
        with self.assertRaisesRegex(SkillForgeError, "untracked"):
            self.manager.abandon(worktree)
        result = subprocess.run(
            ["git", "add", "candidate.py"], cwd=worktree.path,
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run(
            [
                "git", "-c", "user.name=Skill Forge Tests",
                "-c", "user.email=skill-forge-tests@example.invalid",
                "commit", "-m", "candidate commit",
            ],
            cwd=worktree.path, capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.manager.abandon(worktree)
        self.assertFalse(Path(worktree.path).exists())


class SkillForgeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.parent = Path(self.temporary.name)
        self.repo = self.parent / "repo"
        init_git_repo(self.repo)
        self.archive = CandidateArchive(self.parent / "archive")
        self.forge = SkillForge(self.repo, self.archive, generator=StubGenerator())

    def test_prepares_candidate_in_worktree_and_records_archive_metadata(self):
        spec = make_spec()
        candidate = self.forge.prepare_candidate(spec)
        self.assertEqual(candidate.candidate_id, spec.candidate_id)
        details = candidate.metadata["skill_forge"]
        self.assertEqual(candidate.source_branch, f"skillforge/{spec.candidate_id}")
        self.assertEqual(details["runtime_status"], "awaiting_isolated_execution")
        self.assertEqual(details["activation_status"], "not_installed")
        self.assertTrue(details["static_validation"]["passed"])
        worktree_path = self.forge.worktrees.worktree_root / spec.candidate_id
        self.assertEqual(candidate.source_sha256, project_tree_digest(worktree_path))
        self.assertEqual(details["worktree_ref"], spec.candidate_id)
        self.assertNotIn("worktree_path", details)
        self.assertFalse((self.repo / spec.source_path).exists())

    def test_review_packet_contains_source_and_test_files_but_not_activation(self):
        candidate = self.forge.prepare_candidate(make_spec())
        packet = self.forge.review_candidate(candidate.candidate_id)
        self.assertIn("def run", packet.source_files["backtalk/backtalk/features/echo_upper.py"])
        self.assertIn("unittest", packet.source_files["backtalk/tests/test_skillforge_echo_upper.py"])
        self.assertEqual(packet.runtime_status, "awaiting_isolated_execution")
        self.assertEqual(packet.review_status, "pending_review")
        self.assertEqual(packet.installation_status, "not_approved")
        self.assertEqual(packet.activation_status, "not_installed")
        self.assertIn("backtalk/SKILL_FORGE_MANIFEST.json", packet.source_files)
        self.assertIn("+++ b/backtalk/backtalk/features/echo_upper.py", packet.diff)
        self.assertIn("+def run(text: str)", packet.diff)

    def test_execution_requires_a_current_human_code_review(self):
        candidate = self.forge.prepare_candidate(make_spec())
        with self.assertRaisesRegex(SkillForgeError, "human code-review approval"):
            self.forge.evaluate_candidate(candidate.candidate_id)

    def test_missing_secure_backend_fails_closed_without_running_candidate(self):
        candidate = self.forge.prepare_candidate(make_spec())
        self.forge.record_human_review(
            candidate.candidate_id, decision="approved", reason="Reviewed before execution."
        )
        receipt = self.forge.evaluate_candidate(candidate.candidate_id)
        self.assertEqual(receipt.status, "awaiting_isolated_execution")
        self.assertFalse(receipt.passed)
        self.assertFalse(receipt.security_boundary)
        self.assertEqual(receipt.checks, ())

    def test_rejects_human_approval_when_static_validation_failed(self):
        spec = make_spec(required_permissions=("workspace.write",))
        candidate = self.forge.prepare_candidate(spec)
        with self.assertRaisesRegex(SkillForgeError, "failed static validation"):
            self.forge.record_human_review(
                candidate.candidate_id, decision="approved", reason="Reviewed"
            )

    def test_human_review_is_digest_bound_and_append_only(self):
        candidate = self.forge.prepare_candidate(make_spec())
        first = self.forge.record_human_review(
            candidate.candidate_id, decision="approved", reason="Reviewed source."
        )
        second = self.forge.record_human_review(
            candidate.candidate_id, decision="rejected", reason="Later review rejected."
        )
        self.assertNotEqual(first.review_id, second.review_id)
        self.assertEqual(first.source_sha256, candidate.source_sha256)
        packet = self.forge.review_candidate(candidate.candidate_id)
        self.assertEqual(packet.review_status, "rejected")

    def test_source_drift_invalidates_review_and_human_approval(self):
        candidate = self.forge.prepare_candidate(make_spec())
        review = self.forge.record_human_review(
            candidate.candidate_id, decision="approved", reason="Reviewed source."
        )
        source = (
            self.forge.worktrees.worktree_root / candidate.candidate_id
            / "backtalk/backtalk/features/echo_upper.py"
        )
        source.write_text(source.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
        with self.assertRaisesRegex(SkillForgeError, "stale"):
            self.forge.review_candidate(candidate.candidate_id)
        self.assertEqual(review.source_sha256, candidate.source_sha256)

    def test_installation_requires_explicit_confirmation_and_recorded_secure_receipt(self):
        self.forge.execution_backend = PassingExecutionBackend()
        candidate = self.forge.prepare_candidate(make_spec())
        self.forge.record_human_review(
            candidate.candidate_id, decision="approved", reason="Manual code review passed."
        )
        receipt = self.forge.evaluate_candidate(candidate.candidate_id)
        with self.assertRaisesRegex(SkillForgeError, "explicit human confirmation"):
            self.forge.approve_installation(
                candidate.candidate_id,
                source_sha256=candidate.source_sha256,
                runtime_receipt=receipt,
                human_confirmed=False,
            )
        approval = self.forge.approve_installation(
            candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            runtime_receipt=receipt,
            human_confirmed=True,
        )
        self.assertEqual(approval.source_sha256, candidate.source_sha256)
        reviewed_packet = self.forge.review_candidate(candidate.candidate_id)
        self.assertEqual(reviewed_packet.runtime_status, "passed")
        self.assertEqual(reviewed_packet.installation_status, "approved_for_installation")
        self.assertEqual(reviewed_packet.activation_status, "not_installed")
        source = (
            self.forge.worktrees.worktree_root / candidate.candidate_id
            / "backtalk/backtalk/features/echo_upper.py"
        )
        self.assertFalse(source.with_suffix(".approved").exists())

    def test_unrecorded_fabricated_runtime_receipt_is_rejected(self):
        candidate = self.forge.prepare_candidate(make_spec())
        self.forge.record_human_review(
            candidate.candidate_id, decision="approved", reason="Reviewed."
        )
        fake = ExecutionReceipt(
            receipt_id=uuid.uuid4().hex,
            candidate_id=candidate.candidate_id,
            source_sha256=candidate.source_sha256,
            status="passed",
            passed=True,
            security_boundary=True,
            checks=("compileall", "unittest"),
            completed_at="2026-11-03T12:00:00+00:00",
        )
        with self.assertRaisesRegex(SkillForgeError, "not recorded"):
            self.forge.approve_installation(
                candidate.candidate_id,
                source_sha256=candidate.source_sha256,
                runtime_receipt=fake,
                human_confirmed=True,
            )


if __name__ == "__main__":
    unittest.main()
