import asyncio

from nexus.context import DialogueContext


class Session:
    def __init__(self, session_id, pattern_code):
        self.session_id = session_id
        self.pattern_code = pattern_code
        # Pattern object for the current turn (injected at launch; may be a per-request custom pattern)
        self.pattern = None
        # Outbound task info (injected at launch)
        self.task_info = None
        self.history = []
        self.status = None
        self.usr_msg = None
        self.silence_cnt = 0
        self.update = None

        # Dialogue pipeline context: flows through pattern.stages, carrying history, slots, recall results, etc.
        # cxt.user_query is updated before each turn; session state is written back from cxt at turn end
        self.cxt = DialogueContext(session_id=session_id, user_query="")

        # Per-session turn lock: serializes concurrent chat turns on the SAME
        # session (begin_turn/history writes are not otherwise synchronized);
        # different sessions stay fully parallel — LLM-slow turns only block
        # their own session's re-entry, not the event loop. asyncio.Lock
        # since the asyncio rewrite: waiters are queued as tasks on the host
        # loop instead of blocking threads.
        self.turn_lock = asyncio.Lock()
