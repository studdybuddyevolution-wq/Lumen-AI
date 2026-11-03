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
    tool,
)


class ToolRegistryTests(unittest.TestCase):
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

    def _approve(self, feature: Path) -> None:
        feature.with_suffix(".approved").write_text(
            hashlib.sha256(feature.read_bytes()).hexdigest(),
            encoding="ascii",
        )

    def test_feature_reload_replaces_handler_and_deleted_file_unregisters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            feature_dir = Path(temp) / "features"
            feature_dir.mkdir()
            feature = feature_dir / "sample_feature.py"
            schema = (
                '{"type": "object", "properties": {}, '
                '"additionalProperties": false}'
            )
            feature.write_text(
                "from backtalk.registry import tool\n"
                f"@tool('reload_test', 'Reload test tool', {schema}, False)\n"
                "def run():\n    return 'first'\n",
                encoding="utf-8",
            )
            self._approve(feature)
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

            self._approve(feature)
            self.registry.load_features(feature_dir)
            self.assertEqual(
                self.registry.execute("reload_test", {}),
                "second version",
            )

            feature.unlink()
            self.registry.load_features(feature_dir)
            self.assertIsNone(self.registry.get("reload_test"))


if __name__ == "__main__":
    unittest.main()
