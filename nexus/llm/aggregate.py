"""Stream aggregation — reassemble LLMChunks into the legacy non-streaming
dict shape — non-streaming calls are aggregated streaming calls.

Merging rules (OpenAI streaming semantics):
- text: plain concatenation of all chunk texts
- tool_calls: grouped by ``index``; id/name take the first non-empty value,
  arguments is **string-concatenated** across chunks (arguments arrive as
  JSON-fragment deltas, never as mergeable dicts — concatenation first,
  parse later by the consumer)
- finish_reason: the last non-empty value wins
- usage: the last non-empty dict wins (the usage-only tail chunk)
"""

from typing import Any, AsyncIterable, Dict, Iterable, List

from nexus.llm.types import LLMChunk


class _Accumulator:
    """Shared merge state for collect_stream / acollect_stream."""

    def __init__(self):
        self.text_parts: List[str] = []
        self.merged_tools: Dict[int, Dict[str, Any]] = {}
        self.tool_order: List[int] = []
        self.finish_reason = ""
        self.usage: Dict[str, Any] = {}
        self.model = ""

    def add(self, chunk: LLMChunk) -> None:
        if chunk.text:
            self.text_parts.append(chunk.text)
        if chunk.finish_reason:
            self.finish_reason = chunk.finish_reason
        if chunk.usage:
            self.usage = chunk.usage
        if chunk.tool_calls:
            for frag in chunk.tool_calls:
                idx = frag.get("index", 0)
                if idx not in self.merged_tools:
                    self.merged_tools[idx] = {"id": "", "type": "function",
                                              "function": {"name": "",
                                                           "arguments": ""}}
                    self.tool_order.append(idx)
                acc = self.merged_tools[idx]
                frag_fn = frag.get("function", {}) or {}
                if frag.get("id"):
                    acc["id"] = frag["id"]
                name = frag_fn.get("name", "")
                if name:
                    acc["function"]["name"] = name
                args = frag_fn.get("arguments", "")
                if args:
                    acc["function"]["arguments"] += args

    def result(self) -> Dict[str, Any]:
        tool_calls = [self.merged_tools[i] for i in sorted(self.tool_order)]
        result: Dict[str, Any] = {
            "content": "".join(self.text_parts),
            "tool_calls": tool_calls,
            "finish_reason": self.finish_reason,
            "usage": self.usage,
        }
        if self.model:
            result["model"] = self.model
        return result


def collect_stream(chunks: Iterable[LLMChunk]) -> Dict[str, Any]:
    """Aggregate a stream of LLMChunks into the non-streaming result dict.

    Returns the legacy shape: {"content", "tool_calls", "finish_reason",
    "usage"} (+ "model" passthrough when any chunk carried one — none do in
    the current protocol; the field stays for shape compatibility).
    """
    acc = _Accumulator()
    for chunk in chunks:
        acc.add(chunk)
    return acc.result()


async def acollect_stream(chunks: AsyncIterable[LLMChunk]) -> Dict[str, Any]:
    """Async counterpart of collect_stream — same merge rules, same shape."""
    acc = _Accumulator()
    async for chunk in chunks:
        acc.add(chunk)
    return acc.result()
