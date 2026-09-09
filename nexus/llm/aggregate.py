"""Stream aggregation — reassemble LLMChunks into the legacy non-streaming
dict shape (plan-⑤: non-streaming calls are aggregated streaming calls).

Merging rules (OpenAI streaming semantics):
- text: plain concatenation of all chunk texts
- tool_calls: grouped by ``index``; id/name take the first non-empty value,
  arguments is **string-concatenated** across chunks (arguments arrive as
  JSON-fragment deltas, never as mergeable dicts — concatenation first,
  parse later by the consumer)
- finish_reason: the last non-empty value wins
- usage: the last non-empty dict wins (the usage-only tail chunk)
"""

from typing import Any, Dict, Iterable, List

from nexus.llm.types import LLMChunk


def collect_stream(chunks: Iterable[LLMChunk]) -> Dict[str, Any]:
    """Aggregate a stream of LLMChunks into the non-streaming result dict.

    Returns the legacy shape: {"content", "tool_calls", "finish_reason",
    "usage"} (+ "model" passthrough when any chunk carried one — none do in
    the current protocol; the field stays for shape compatibility).
    """
    text_parts: List[str] = []
    merged_tools: Dict[int, Dict[str, Any]] = {}
    tool_order: List[int] = []
    finish_reason = ""
    usage: Dict[str, Any] = {}
    model = ""

    for chunk in chunks:
        if chunk.text:
            text_parts.append(chunk.text)
        if chunk.finish_reason:
            finish_reason = chunk.finish_reason
        if chunk.usage:
            usage = chunk.usage
        if chunk.tool_calls:
            for frag in chunk.tool_calls:
                idx = frag.get("index", 0)
                if idx not in merged_tools:
                    merged_tools[idx] = {"id": "", "type": "function",
                                         "function": {"name": "",
                                                      "arguments": ""}}
                    tool_order.append(idx)
                acc = merged_tools[idx]
                frag_fn = frag.get("function", {}) or {}
                if frag.get("id"):
                    acc["id"] = frag["id"]
                name = frag_fn.get("name", "")
                if name:
                    acc["function"]["name"] = name
                args = frag_fn.get("arguments", "")
                if args:
                    acc["function"]["arguments"] += args

    tool_calls = [merged_tools[i] for i in sorted(tool_order)]
    result: Dict[str, Any] = {
        "content": "".join(text_parts),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "usage": usage,
    }
    if model:
        result["model"] = model
    return result
