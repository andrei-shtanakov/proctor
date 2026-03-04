"""Tests for Agent Runtime — LLM loop with tool calling."""

from typing import Any

import pytest

from proctor.workers.runtime import (
    AgentResult,
    AgentRuntime,
    ToolDef,
    ToolResult,
)

# ── Helpers ──────────────────────────────────────────────────────


def make_text_response(content: str) -> dict[str, Any]:
    """Create a text LLM response."""
    return {"type": "text", "content": content}


def make_tool_call(
    tool_name: str, tool_args: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Create a tool_call LLM response."""
    return {
        "type": "tool_call",
        "tool_name": tool_name,
        "tool_args": tool_args or {},
    }


# ── Model Tests ──────────────────────────────────────────────────


class TestToolDef:
    def test_creation(self) -> None:
        async def handler() -> str:
            return "ok"

        td = ToolDef(name="test", description="A test tool", handler=handler)
        assert td.name == "test"
        assert td.description == "A test tool"

    def test_handler_stored(self) -> None:
        async def my_handler(x: int) -> str:
            return str(x)

        td = ToolDef(name="calc", description="calc", handler=my_handler)
        assert td.handler is my_handler


class TestToolResult:
    def test_success(self) -> None:
        tr = ToolResult(tool_name="search", output="found it")
        assert tr.tool_name == "search"
        assert tr.output == "found it"
        assert tr.error is None

    def test_error(self) -> None:
        tr = ToolResult(tool_name="search", error="not found")
        assert tr.output is None
        assert tr.error == "not found"


class TestAgentResult:
    def test_creation(self) -> None:
        ar = AgentResult(output="hello", turns=1)
        assert ar.output == "hello"
        assert ar.turns == 1
        assert ar.tool_calls == []

    def test_with_tool_calls(self) -> None:
        calls = [ToolResult(tool_name="t1", output="ok")]
        ar = AgentResult(output="done", turns=2, tool_calls=calls)
        assert len(ar.tool_calls) == 1
        assert ar.tool_calls[0].tool_name == "t1"


# ── AgentRuntime Tests ───────────────────────────────────────────


class TestAgentRuntimeNoTools:
    """REQ-009: simple completion without tools."""

    @pytest.mark.anyio
    async def test_simple_text_response(self) -> None:
        """LLM returns text immediately → 1 turn, no tool calls."""
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return make_text_response("Hello, world!")

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[])
        result = await runtime.run("Say hello")

        assert result.output == "Hello, world!"
        assert result.turns == 1
        assert result.tool_calls == []
        assert call_count == 1

    @pytest.mark.anyio
    async def test_no_tools_passed_to_llm(self) -> None:
        """When no tools defined, LLM receives None for tools."""
        received_tools = None

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal received_tools
            received_tools = tools
            return make_text_response("ok")

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[])
        await runtime.run("test")

        assert received_tools is None

    @pytest.mark.anyio
    async def test_instruction_in_messages(self) -> None:
        """User instruction appears as first message."""
        captured_messages: list[dict[str, Any]] = []

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            captured_messages.extend(messages)
            return make_text_response("done")

        runtime = AgentRuntime(llm_fn=mock_llm)
        await runtime.run("Find RISC-V info")

        assert captured_messages[0]["role"] == "user"
        assert captured_messages[0]["content"] == "Find RISC-V info"


class TestAgentRuntimeToolCalls:
    """REQ-009: tool call and response."""

    @pytest.mark.anyio
    async def test_tool_call_then_text(self) -> None:
        """LLM calls tool, gets result, then returns text → 2 turns."""
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_tool_call("get_weather", {"city": "Tbilisi"})
            return make_text_response("Weather in Tbilisi: sunny, 25C")

        async def get_weather(city: str) -> str:
            return f"sunny, 25C in {city}"

        tool = ToolDef(
            name="get_weather",
            description="Get weather for a city",
            handler=get_weather,
        )
        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool])
        result = await runtime.run("What's the weather in Tbilisi?")

        assert result.output == "Weather in Tbilisi: sunny, 25C"
        assert result.turns == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "get_weather"
        assert "sunny" in (result.tool_calls[0].output or "")
        assert result.tool_calls[0].error is None

    @pytest.mark.anyio
    async def test_multiple_tool_calls(self) -> None:
        """LLM calls two tools sequentially before returning text."""
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_tool_call("search", {"q": "RISC-V"})
            if call_count == 2:
                return make_tool_call("summarize", {"text": "found"})
            return make_text_response("Summary: RISC-V is an ISA")

        async def search(q: str) -> str:
            return f"Results for {q}"

        async def summarize(text: str) -> str:
            return f"Summary of {text}"

        tools = [
            ToolDef(name="search", description="Search", handler=search),
            ToolDef(
                name="summarize",
                description="Summarize",
                handler=summarize,
            ),
        ]
        runtime = AgentRuntime(llm_fn=mock_llm, tools=tools)
        result = await runtime.run("Research RISC-V")

        assert result.turns == 3
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].tool_name == "search"
        assert result.tool_calls[1].tool_name == "summarize"

    @pytest.mark.anyio
    async def test_tool_defs_passed_to_llm(self) -> None:
        """Tool definitions are passed to the LLM."""
        received_tools: list[dict[str, Any]] | None = None

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal received_tools
            received_tools = tools
            return make_text_response("ok")

        async def handler() -> str:
            return "ok"

        tool = ToolDef(name="my_tool", description="Does stuff", handler=handler)
        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool])
        await runtime.run("test")

        assert received_tools is not None
        assert len(received_tools) == 1
        assert received_tools[0]["name"] == "my_tool"
        assert received_tools[0]["description"] == "Does stuff"

    @pytest.mark.anyio
    async def test_tool_result_in_messages(self) -> None:
        """Tool result is added to messages for the next LLM call."""
        all_messages: list[list[dict[str, Any]]] = []

        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            all_messages.append(list(messages))
            if call_count == 1:
                return make_tool_call("echo", {"text": "ping"})
            return make_text_response("pong")

        async def echo(text: str) -> str:
            return text

        tool = ToolDef(name="echo", description="Echo", handler=echo)
        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool])
        await runtime.run("test")

        # Second call should have tool result in messages
        second_call_msgs = all_messages[1]
        assert second_call_msgs[-1]["role"] == "tool"
        assert second_call_msgs[-1]["tool_name"] == "echo"
        assert second_call_msgs[-1]["content"] == "ping"


class TestAgentRuntimeMaxTurns:
    """REQ-009: max turns limit."""

    @pytest.mark.anyio
    async def test_max_turns_stops_loop(self) -> None:
        """Loop stops at max_turns even if LLM keeps requesting tools."""
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return make_tool_call("infinite", {})

        async def infinite() -> str:
            return "still going"

        tool = ToolDef(name="infinite", description="Never stops", handler=infinite)
        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool], max_turns=3)
        result = await runtime.run("Go forever")

        assert result.turns == 3
        assert call_count == 3
        assert len(result.tool_calls) == 3

    @pytest.mark.anyio
    async def test_max_turns_returns_last_content(self) -> None:
        """When max turns hit, returns the last tool output."""

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            return make_tool_call("step", {})

        async def step() -> str:
            return "step output"

        tool = ToolDef(name="step", description="Step", handler=step)
        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool], max_turns=2)
        result = await runtime.run("test")

        assert result.turns == 2
        assert result.output == "step output"

    @pytest.mark.anyio
    async def test_max_turns_default(self) -> None:
        """Default max_turns is 10."""

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            return make_text_response("done")

        runtime = AgentRuntime(llm_fn=mock_llm)
        assert runtime.max_turns == 10


class TestAgentRuntimeUnknownTool:
    """REQ-009: unknown tool returns error."""

    @pytest.mark.anyio
    async def test_unknown_tool_error_result(self) -> None:
        """Unknown tool name produces error ToolResult, loop continues."""
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_tool_call("nonexistent_tool", {"x": 1})
            return make_text_response("Handled the error")

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[])
        result = await runtime.run("test")

        assert result.output == "Handled the error"
        assert result.turns == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].tool_name == "nonexistent_tool"
        assert result.tool_calls[0].error is not None
        assert "Unknown tool" in result.tool_calls[0].error

    @pytest.mark.anyio
    async def test_unknown_tool_error_in_messages(self) -> None:
        """Error from unknown tool is fed back to LLM."""
        all_messages: list[list[dict[str, Any]]] = []
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            all_messages.append(list(messages))
            if call_count == 1:
                return make_tool_call("bad_tool", {})
            return make_text_response("recovered")

        runtime = AgentRuntime(llm_fn=mock_llm, tools=[])
        await runtime.run("test")

        second_msgs = all_messages[1]
        tool_msg = second_msgs[-1]
        assert tool_msg["role"] == "tool"
        assert "Unknown tool" in tool_msg["content"]


class TestAgentRuntimeToolErrors:
    """Tool handler exceptions are caught and reported."""

    @pytest.mark.anyio
    async def test_tool_exception_caught(self) -> None:
        """Tool raising an exception produces error ToolResult."""
        call_count = 0

        async def mock_llm(
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
        ) -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return make_tool_call("failing", {})
            return make_text_response("Recovered from error")

        async def failing() -> str:
            raise ValueError("Something broke")

        tool = ToolDef(name="failing", description="Always fails", handler=failing)
        runtime = AgentRuntime(llm_fn=mock_llm, tools=[tool])
        result = await runtime.run("test")

        assert result.turns == 2
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].error is not None
        assert "Something broke" in result.tool_calls[0].error
        assert result.output == "Recovered from error"


class TestPublicExports:
    def test_import_from_workers(self) -> None:
        from proctor.workers import (
            AgentResult,
            AgentRuntime,
            ToolDef,
            ToolResult,
        )

        assert AgentRuntime is not None
        assert ToolDef is not None
        assert ToolResult is not None
        assert AgentResult is not None
