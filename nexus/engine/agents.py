"""
Pluggable executor interface for AGENT modules — a reserved swap point for
agent backends.

Agent execution is currently hard-wired to loop.run_agent (the ReAct tool
loop). This module abstracts "how to run an agent module" behind the
AgentRunner protocol: AgentHandler depends only on the protocol, and the
default LoopAgentRunner delegates to loop.run_agent with unchanged behavior.
Later agent backends (planner-executor, external agent services, ...) just
provide a new implementation to inject — the chat orchestration layer stays
untouched.

No registry here (CLAUDE.md: no new global singletons) — the injection point
is the optional agent_runner parameter of chat()/chat_turn().
"""

import logging
from typing import Any, Dict, Protocol, runtime_checkable

from nexus.engine.loop import TurnResult, run_agent
from nexus.engine.session import Session

logger = logging.getLogger(__name__)


@runtime_checkable
class AgentRunner(Protocol):
    """Plugin interface for AGENT module executors.

    Implementor contract:
    - Input: session (with cxt history/slots), module, resolved llm_config
    - Output: TurnResult (reply is the response; on a transfer turn the
      reply is empty and the jump event is written to cxt.actions, consumed
      by the chat layer's hop loop)
    - With force_close=True, produce no new transfers (forced close once
      max_hops is exhausted)
    """

    def run(self, session: Session, module,
            llm_config: Dict[str, Any], force_close: bool = False) -> TurnResult:
        ...


class LoopAgentRunner:
    """Default implementation: delegates to loop.run_agent (the only execution path today)."""

    def run(self, session: Session, module,
            llm_config: Dict[str, Any], force_close: bool = False) -> TurnResult:
        return run_agent(session, module, llm_config, force_close=force_close)
