from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from backtalk.registry import (
    ToolRegistry,
    ToolRegistrationError,
    ToolValidationError,
    approve_feature_module,
    record_feature_verification,
    tool,
)


class ToolRegistryTests(unittest.TestCase):
    def test_existing_tools_register_through_builtin_adapter(self) -> None:
        from backtalk.features import core_tools  # noqa: F401
        from backtalk.registry import DEFAULT_REGISTRY
        from backtalk.tools import TOOL_DEFINITIONS

        registered = {item.name for item in DEFAULT_REGISTRY.list_tools()}
        expected = {
            item["function"]["name"] for item in TOOL_DEFINITIONS
        }
        self.assertTrue(expected.issubset(registered))
        self.assertEqual(len(expected), len(TOOL_DEFINITIONS))

    def setUp(self) -> None:
        self.registry = ToolRegistry()

    def test_decorator_registers_and_executes_tool(self) -> None:
        @tool(
            name="add_values",
            description="Add two numbers",
            params_schema={
                "type": "object",
                "properties": {
                    "left": {"type": "integer"},
                    "right": {"type": "integer"},
                },
                "required": ["left", "right"],
                "additionalProperties": False,
            },
            registry=self.registry,
        )
        def add_values(left: int, right: int) -> int:
            return left + right

        self.assertEqual(self.registry.execute("add_values", {"left": 2, "right": 3}), 5)
        self.assertIsNotNone(self.registry.get("add_values"))

    def test_invalid_schema_is_rejected(self) -> None:
        with self.assertRaises(ToolRegistrationError):
            self.registry.register(
                name="bad_schema",
                description="Bad schema",
                params_schema={"type": "object", "properties": {"x": {"type": "not-a-type"}}},
                mutating=False,
                handler=lambda **kwargs: None,
            )

    def test_duplicate_names_are_rejected(self) -> None:
        schema = {"type": "object", "properties": {}}
        self.registry.register(
            name="same_name", description="One", params_schema=schema,
            mutating=False, handler=lambda **kwargs: "one",
        )
        with self.assertRaises(ToolRegistrationError):
            self.registry.register(
                name="same_name", description="Two", params_schema=schema,
                mutating=False, handler=lambda **kwargs: "two",
            )

    def test_invalid_arguments_return_clear_error(self) -> None:
        self.registry.register(
            name="needs_text",
            description="Needs text",
            params_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            mutating=False,
            handler=lambda text: text,
        )
        with self.assertRaisesRegex(ToolValidationError, "text"):
            self.registry.execute("needs_text", {"text": 123})

    def _verify_and_approve(self, feature: Path) -> None:
        digest = hashlib.sha256(feature.read_bytes()).hexdigest()
        receipt = {
            "receipt_id": "unit-test-receipt",
            "passed": True,
            "subject_sha256": digest,
            "source_sha256": "test-workspace-digest",
            "snapshot_sha256": "test-snapshot-digest",
            "checks": [{"name": "unittest", "passed": True}],
            "finished_at": "2026-11-03T00:00:00+00:00",
            "isolation": "test-fixture",
            "security_boundary": False,
        }
        record_feature_verification(feature, receipt)
        approve_feature_module(feature)

    def test_approval_is_refused_without_a_passing_matching_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature = Path(temp) / "sample.py"
            feature.write_text("VALUE = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sandbox verification"):
                approve_feature_module(feature)

            digest = hashlib.sha256(feature.read_bytes()).hexdigest()
            failed = {"receipt_id": "failed", "passed": False, "subject_sha256": digest}
            with self.assertRaisesRegex(ValueError, "passing sandbox receipt"):
                record_feature_verification(feature, failed)

            passed = {
                "receipt_id": "passed",
                "passed": True,
                "subject_sha256": "wrong-digest",
            }
            with self.assertRaisesRegex(ValueError, "does not match"):
                record_feature_verification(feature, passed)

    def test_approved_feature_without_verified_receipt_is_not_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature_dir = Path(temp) / "features"
            feature_dir.mkdir()
            feature = feature_dir / "unverified_feature.py"
            feature.write_text(
                "from backtalk.registry import tool\n"
                "@tool('unverified_tool', 'Must not load', "
                "{'type': 'object', 'properties': {}}, False)\n"
                "def run():\n    return 'unsafe'\n",
                encoding="utf-8",
            )
            digest = hashlib.sha256(feature.read_bytes()).hexdigest()
            feature.with_suffix(".approved").write_text(digest, encoding="ascii")
            result = self.registry.load_features(feature_dir)
            self.assertIn("sandbox verification", result[feature.name])
            self.assertIsNone(self.registry.get("unverified_tool"))

    def test_feature_reload_replaces_handler_and_deleted_file_unregisters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature_dir = Path(temp) / "features"
            feature_dir.mkdir()
            feature = feature_dir / "sample_feature.py"
            # The feature file is Python source, so build a Python dict
            # representation rather than inserting raw JSON (JSON uses
            # lowercase true/false/null, which are not Python literals).
            schema = repr({
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            })
            feature.write_text(
                "from backtalk.registry import tool\n"
                f"@tool('reload_test', 'Reload test tool', {schema}, False)\n"
                "def run():\n    return 'first'\n",
                encoding="utf-8",
            )
            self._verify_and_approve(feature)
            self.registry.load_features(feature_dir)
            self.assertEqual(self.registry.execute("reload_test", {}), "first")

            feature.write_text(
                "from backtalk.registry import tool\n"
                f"@tool('reload_test', 'Reload test tool', {schema}, False)\n"
                "def run():\n    return 'second version'\n",
                encoding="utf-8",
            )
            time.sleep(0.02)
            self.registry.load_features(feature_dir)
            self.assertEqual(self.registry.execute("reload_test", {}), "first")

            self._verify_and_approve(feature)
            self.registry.load_features(feature_dir)
            self.assertEqual(
                self.registry.execute("reload_test", {}),
                "second version",
            )

            # Removing the approval marker revokes the loaded module.
            feature.with_suffix(".approved").unlink()
            self.registry.load_features(feature_dir)
            self.assertIsNone(self.registry.get("reload_test"))

            # Re-approve, load again, then verify deleting an active feature unregisters it.
            self._verify_and_approve(feature)
            self.registry.load_features(feature_dir)
            self.assertEqual(self.registry.execute("reload_test", {}), "second version")
            feature.unlink()
            feature.with_suffix(".approved").unlink()
            self.registry.load_features(feature_dir)
            self.assertIsNone(self.registry.get("reload_test"))


if __name__ == "__main__":
    unittest.main()
