from __future__ import annotations

import unittest

from backtalk.registry import ToolDefinition
from backtalk.router import ToolRouter


def definition(name: str, description: str, properties: dict | None = None) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        params_schema={"type": "object", "properties": properties or {}},
        mutating=False,
        handler=lambda **kwargs: None,
        source="test",
    )


class ToolRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.router = ToolRouter(max_tools=8, min_tools=5)
        self.tools = [
            definition("open_application", "Open a desktop application", {"app": {"type": "string"}}),
            definition("create_file", "Create a new text file", {"path": {"type": "string"}}),
            definition("edit_file", "Edit an existing file", {"path": {"type": "string"}}),
            definition("run_powershell", "Run a Windows PowerShell command", {"command": {"type": "string"}}),
            definition("list_directory", "List folder contents", {"path": {"type": "string"}}),
            definition("read_file", "Read a UTF-8 file", {"path": {"type": "string"}}),
            definition("search_memory", "Search the personal memory vault", {"query": {"type": "string"}}),
            definition("save_memory", "Save a note into memory", {"content": {"type": "string"}}),
            definition("get_active_priorities", "Read active priorities", {}),
            definition("barehands_board_state", "Inspect the gesture board", {}),
            definition("weather", "Read a weather forecast", {}),
        ]

    def test_router_limits_tool_context(self) -> None:
        chosen = self.router.select("open Notepad", self.tools)
        self.assertLessEqual(len(chosen), 8)
        self.assertGreaterEqual(len(chosen), 5)
        self.assertEqual(chosen[0].name, "open_application")

    def test_router_prefers_memory_tools_for_memory_request(self) -> None:
        chosen = self.router.select("search my memory vault", self.tools)
        self.assertEqual(chosen[0].name, "search_memory")

    def test_router_handles_empty_tool_list(self) -> None:
        self.assertEqual(self.router.select("open Notepad", []), [])


if __name__ == "__main__":
    unittest.main()
