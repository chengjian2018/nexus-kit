"""LLM streaming chunk protocol.

A provider's native streaming implementation yields LLMChunk objects — the
structured unit the aggregator (llm/aggregate.py::collect_stream) reassembles
into the legacy non-streaming dict shape. Fields:

- text: content delta for this chunk ("" when the chunk carries only
  tool_call fragments / usage)
- tool_calls: OpenAI delta.tool_calls fragments as-is — each entry carries
  the split fields with ``index`` selecting the accumulating slot; the
  aggregator merges per index (id/name from the first non-empty, arguments
  string-concatenated — the OpenAI streaming semantics)
- finish_reason: carried by the final content chunk ("" until then)
- usage: carried by the usage-only tail chunk (stream_options.include_usage;
  note that chunk's ``choices`` is an empty array)
- reasoning: thinking-model delta (``delta.reasoning_content`` — GLM/Qwen
  thinking mode); NOT aggregated into the legacy dict (aggregation keeps the
  non-streaming shape byte-stable), streamed consumers forward it as
  thinking events
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class LLMChunk:
    """One structured chunk of a streamed LLM response."""

    text: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""
