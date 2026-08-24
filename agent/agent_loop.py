"""Multi-hop agentic retrieval.

Phase 2 answered every question with exactly one graph query. That works when
the question maps cleanly onto one traversal and fails when it does not — a
question needing a name looked up before it can be queried, or one phrased in
terms of behaviour rather than structure, has nowhere to go.

Here the model chooses tools and iterates: query the graph, look at what came
back, search semantically, read the actual source, then answer. Each iteration
sees the previous results, so a dead end is recoverable rather than fatal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from agent.few_shot import render_examples
from agent.provider import LLMProvider, Message, ToolCall, Usage
from agent.query_agent import _tidy_answer
from agent.schema_prompt import schema_description
from retrieval.locations import Location, dedupe
from retrieval.tools import RetrievalTools

logger = logging.getLogger(__name__)

#: Each hop re-sends the whole conversation, so the ceiling bounds both latency
#: and token spend. Four is enough for "find X, then ask about X" chains.
MAX_HOPS = 6
TURN_MAX_TOKENS = 2000

#: Cypher examples carried in the loop's system prompt. Kept small because that
#: prompt is re-sent on every hop; the single-shot path uses the full set.
EXAMPLES_IN_LOOP = 4

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "graph_query",
            "description": (
                "Run a read-only Cypher query against the code graph. Best for "
                "structural questions: callers, callees, inheritance, imports, "
                "what a file defines. Must be scoped with {repo_id: $repo_id}."
            ),
            "parameters": {
                "type": "object",
                "properties": {"cypher": {"type": "string"}},
                "required": ["cypher"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Find definitions by meaning rather than by name, using their "
                "docstrings and signatures. Use when the question describes "
                "behaviour ('where is retry handled') and you do not yet know "
                "which identifier to query for. Returns each match with its "
                "callers and callees."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_code",
            "description": (
                "Read the actual source of a function, method or class by its "
                "name or qualified name. Use when the graph says what connects "
                "to what but the question needs the implementation itself."
            ),
            "parameters": {
                "type": "object",
                "properties": {"qualified_name": {"type": "string"}},
                "required": ["qualified_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "Regex search over raw source. Last resort for things the graph "
                "does not model: string literals, comments, decorators, config."
            ),
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
]

_SYSTEM = """\
You answer questions about a Python codebase using a knowledge graph of its \
structure, a semantic index of its docstrings, and the source itself.

Work by investigation, not by guessing:
- Structural question with a known name ("what calls X") -> graph_query.
- Behavioural or vague question ("where is retry handled") -> semantic_search \
first to find the names, then graph_query to trace them.
- If a query returns nothing, do not give up. Try a different label, match on \
`name` instead of `qualified_name`, search semantically, or grep.
- Read the source when the question is about what code *does*, not just how it \
connects.

When you have enough, answer in plain prose:
- Ground every claim in tool output. Never invent names, files, or line numbers.
- Cite as `path/to/file.py:42`, inline and once each. Only cite a line number \
that appeared in tool output; if you do not have one, cite the file alone. \
Never write `:0`.
- Say plainly if the tools could not answer the question. A wrong answer is \
worse than an admitted gap.
- This tool is read-only. Never supply write queries or database advice.
"""


@dataclass
class Hop:
    tool: str
    argument: str
    ok: bool
    result: str
    locations: list[Location] = field(default_factory=list)

    def summary(self, width: int = 160) -> str:
        status = "" if self.ok else " [failed]"
        return f"{self.tool}({self.argument[:width]}){status}"


@dataclass
class AgentResult:
    question: str
    answer: str
    hops: list[Hop] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    hit_hop_limit: bool = False

    @property
    def locations(self) -> list[Location]:
        """Every source location this run put in front of the model."""
        return dedupe([loc for hop in self.hops for loc in hop.locations])


class MultiHopAgent:
    """Lets the model choose tools and iterate until it can answer."""

    def __init__(
        self,
        provider: LLMProvider,
        tools: RetrievalTools,
        max_hops: int = MAX_HOPS,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.max_hops = max_hops

    def _initial_messages(self, question: str) -> list[Message]:
        # This block is re-sent on every hop, so its size multiplies by the hop
        # count. Only the examples that generalise are worth that.
        context = (
            f"{_SYSTEM}\n\n{schema_description()}\n\n{render_examples(limit=EXAMPLES_IN_LOOP)}\n\n"
            f"The repository id is already bound to $repo_id in every query."
        )
        return [
            Message(role="system", content=context),
            Message(role="user", content=question),
        ]

    def _dispatch(self, call: ToolCall) -> Hop:
        arguments = call.arguments
        if "__malformed__" in arguments:
            return Hop(call.name, str(arguments), False, "Arguments were not valid JSON.")

        try:
            if call.name == "graph_query":
                argument = arguments.get("cypher", "")
                result = self.tools.graph_query(argument)
            elif call.name == "semantic_search":
                argument = arguments.get("query", "")
                result = self.tools.semantic_search(argument)
            elif call.name == "read_code":
                argument = arguments.get("qualified_name", "")
                result = self.tools.read_code(argument)
            elif call.name == "grep":
                argument = arguments.get("pattern", "")
                result = self.tools.grep(argument)
            else:
                return Hop(call.name, str(arguments), False, f"No tool named {call.name!r}.")
        except Exception as exc:
            # Reported back to the model as text; a broken tool call should not
            # end the investigation.
            logger.warning("tool %s raised", call.name, exc_info=True)
            return Hop(call.name, str(arguments), False, f"Tool failed: {exc}")

        return Hop(call.name, argument, result.ok, result.text, result.locations)

    def answer(self, question: str) -> AgentResult:
        messages = self._initial_messages(question)
        result = AgentResult(question=question, answer="")

        for hop_number in range(1, self.max_hops + 1):
            response = self.provider.converse(
                messages, TOOL_SPECS, max_tokens=TURN_MAX_TOKENS, effort="medium"
            )
            result.usage.record(f"hop#{hop_number}", response)

            if not response.tool_calls:
                result.answer = _tidy_answer(response.text)
                return result

            messages.append(
                Message(
                    role="assistant", content=response.text, tool_calls=response.tool_calls
                )
            )
            for call in response.tool_calls:
                hop = self._dispatch(call)
                result.hops.append(hop)
                logger.info("hop %d: %s", hop_number, hop.summary())
                messages.append(
                    Message(role="tool", content=hop.result, tool_call_id=call.id)
                )

        # Out of hops: ask for an answer from what was already gathered rather
        # than returning nothing.
        result.hit_hop_limit = True
        messages.append(
            Message(
                role="user",
                content=(
                    "You have run out of investigation steps. Answer now from what "
                    "the tools returned, and say plainly what remains unresolved."
                ),
            )
        )
        final = self.provider.converse(messages, [], max_tokens=TURN_MAX_TOKENS, effort="low")
        result.usage.record("final", final)
        result.answer = _tidy_answer(final.text) or (
            f"I could not answer within {self.max_hops} investigation steps. "
            f"Tools run: {', '.join(hop.tool for hop in result.hops)}."
        )
        return result
