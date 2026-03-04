"""Agent Runtime — LLM loop with tool calling.

Receives an instruction, calls LLM, executes tool calls, feeds results
back, and repeats until a text response or max_turns is reached.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# LLM function: takes (messages, tool_defs) -> response dict
# Response must be {"type": "text", "content": "..."} or
# {"type": "tool_call", "tool_name": "...", "tool_args": {...}}
LLMFn = Callable[
    [list[dict[str, Any]], list[dict[str, Any]] | None],
    Awaitable[dict[str, Any]],
]


class ToolDef(BaseModel):
    """Definition of a tool available to the agent."""

    name: str
    description: str
    handler: Any  # async callable(args) -> str


class ToolResult(BaseModel):
    """Result of a single tool execution."""

    tool_name: str
    output: str | None = None
    error: str | None = None


class AgentResult(BaseModel):
    """Final result from an agent run."""

    output: str
    turns: int
    tool_calls: list[ToolResult] = Field(default_factory=list)


class AgentRuntime:
    """LLM agent loop with tool calling support.

    Loops between LLM calls and tool executions until the LLM
    returns a text response or max_turns is exhausted.
    """

    def __init__(
        self,
        llm_fn: LLMFn,
        tools: list[ToolDef] | None = None,
        max_turns: int = 10,
    ) -> None:
        self.llm_fn = llm_fn
        self.tools = {t.name: t for t in (tools or [])}
        self.max_turns = max_turns

    async def run(self, instruction: str) -> AgentResult:
        """Execute the agent loop.

        Builds messages starting with the user instruction, calls
        the LLM, and processes tool calls until a text response
        or max_turns is reached.
        """
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": instruction},
        ]
        tool_defs = self._build_tool_defs()
        tool_results: list[ToolResult] = []
        turns = 0

        for turns in range(1, self.max_turns + 1):
            response = await self.llm_fn(messages, tool_defs or None)

            if response.get("type") == "text":
                return AgentResult(
                    output=response.get("content", ""),
                    turns=turns,
                    tool_calls=tool_results,
                )

            if response.get("type") == "tool_call":
                tool_name = response.get("tool_name", "")
                tool_args = response.get("tool_args", {})

                result = await self._call_tool(tool_name, tool_args)
                tool_results.append(result)

                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_call": {
                            "tool_name": tool_name,
                            "tool_args": tool_args,
                        },
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": tool_name,
                        "content": result.output or result.error or "",
                    }
                )
                continue

            # Unknown response type — treat as text
            return AgentResult(
                output=str(response.get("content", "")),
                turns=turns,
                tool_calls=tool_results,
            )

        # Max turns exhausted
        last_content = ""
        for msg in reversed(messages):
            if msg.get("content"):
                last_content = str(msg["content"])
                break

        return AgentResult(
            output=last_content,
            turns=self.max_turns,
            tool_calls=tool_results,
        )

    async def _call_tool(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> ToolResult:
        """Execute a tool by name. Returns error for unknown tools."""
        tool = self.tools.get(tool_name)
        if tool is None:
            logger.warning("Unknown tool requested: %s", tool_name)
            return ToolResult(
                tool_name=tool_name,
                error=f"Unknown tool: {tool_name}",
            )

        try:
            output = await tool.handler(**tool_args)
            return ToolResult(tool_name=tool_name, output=str(output))
        except Exception as exc:
            logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
            return ToolResult(
                tool_name=tool_name,
                error=f"Tool error: {exc}",
            )

    def _build_tool_defs(self) -> list[dict[str, Any]]:
        """Build tool definitions for the LLM."""
        return [
            {"name": t.name, "description": t.description} for t in self.tools.values()
        ]
