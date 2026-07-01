"""Thread-safe, in-memory conversation history manager for multi-turn Slack interactions."""

import os
import threading
import time
from dataclasses import dataclass, field

CONVERSATION_TTL_SECONDS = int(os.getenv("CONVERSATION_TTL_SECONDS", "7200"))
MAX_MESSAGES_PER_THREAD = int(os.getenv("MAX_MESSAGES_PER_THREAD", "20"))


@dataclass
class ConversationState:
    channel_id: str
    thread_ts: str
    messages: list[dict] = field(default_factory=list)
    last_activity: float = field(default_factory=time.time)


class ConversationManager:
    """Manages per-thread conversation history with TTL-based expiry."""

    def __init__(
        self,
        ttl_seconds: int = CONVERSATION_TTL_SECONDS,
        max_messages: int = MAX_MESSAGES_PER_THREAD,
    ):
        self._conversations: dict[str, ConversationState] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_messages = max_messages

    def _key(self, channel_id: str, thread_ts: str) -> str:
        return f"{channel_id}:{thread_ts}"

    def _evict_expired(self) -> None:
        now = time.time()
        expired = [
            k
            for k, v in self._conversations.items()
            if now - v.last_activity > self._ttl
        ]
        for k in expired:
            del self._conversations[k]

    def _trim(self, state: ConversationState) -> None:
        if len(state.messages) > self._max_messages:
            state.messages = state.messages[-self._max_messages :]

    def _get_or_create(self, channel_id: str, thread_ts: str) -> ConversationState:
        key = self._key(channel_id, thread_ts)
        if key not in self._conversations:
            self._conversations[key] = ConversationState(
                channel_id=channel_id,
                thread_ts=thread_ts,
            )
        state = self._conversations[key]
        state.last_activity = time.time()
        return state

    def append_user_message(self, channel_id: str, thread_ts: str, text: str) -> None:
        with self._lock:
            self._evict_expired()
            state = self._get_or_create(channel_id, thread_ts)
            state.messages.append({"role": "user", "content": text})
            self._trim(state)

    def append_assistant_message(
        self, channel_id: str, thread_ts: str, text: str
    ) -> None:
        with self._lock:
            self._evict_expired()
            state = self._get_or_create(channel_id, thread_ts)
            state.messages.append({"role": "assistant", "content": text})
            self._trim(state)

    def get_messages(self, channel_id: str, thread_ts: str) -> list[dict]:
        with self._lock:
            key = self._key(channel_id, thread_ts)
            state = self._conversations.get(key)
            if state is None:
                return []
            return list(state.messages)

    def clear(self, channel_id: str, thread_ts: str) -> None:
        with self._lock:
            key = self._key(channel_id, thread_ts)
            self._conversations.pop(key, None)
