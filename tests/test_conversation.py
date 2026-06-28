"""Unit tests for ConversationManager."""

import time
import threading
from bugzooka.core.conversation import ConversationManager


class TestConversationManager:
    def test_new_conversation_created(self):
        mgr = ConversationManager()
        mgr.append_user_message("C123", "ts1", "hello")
        msgs = mgr.get_messages("C123", "ts1")
        assert len(msgs) == 1
        assert msgs[0] == {"role": "user", "content": "hello"}

    def test_append_user_and_assistant(self):
        mgr = ConversationManager()
        mgr.append_user_message("C123", "ts1", "question")
        mgr.append_assistant_message("C123", "ts1", "answer")
        msgs = mgr.get_messages("C123", "ts1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    def test_separate_threads_have_separate_history(self):
        mgr = ConversationManager()
        mgr.append_user_message("C123", "ts1", "thread 1")
        mgr.append_user_message("C123", "ts2", "thread 2")
        msgs1 = mgr.get_messages("C123", "ts1")
        msgs2 = mgr.get_messages("C123", "ts2")
        assert len(msgs1) == 1
        assert len(msgs2) == 1
        assert msgs1[0]["content"] == "thread 1"
        assert msgs2[0]["content"] == "thread 2"

    def test_get_messages_returns_copy(self):
        mgr = ConversationManager()
        mgr.append_user_message("C123", "ts1", "hello")
        msgs = mgr.get_messages("C123", "ts1")
        msgs.append({"role": "user", "content": "injected"})
        assert len(mgr.get_messages("C123", "ts1")) == 1

    def test_get_messages_empty_for_unknown_thread(self):
        mgr = ConversationManager()
        assert mgr.get_messages("C123", "unknown") == []

    def test_max_messages_trimmed(self):
        mgr = ConversationManager(max_messages=4)
        for i in range(6):
            mgr.append_user_message("C123", "ts1", f"msg {i}")
        msgs = mgr.get_messages("C123", "ts1")
        assert len(msgs) == 4
        assert msgs[0]["content"] == "msg 2"
        assert msgs[-1]["content"] == "msg 5"

    def test_ttl_expiry(self):
        mgr = ConversationManager(ttl_seconds=0)
        mgr.append_user_message("C123", "ts1", "old message")
        time.sleep(0.01)
        mgr.append_user_message("C123", "ts2", "new message")
        assert mgr.get_messages("C123", "ts1") == []
        assert len(mgr.get_messages("C123", "ts2")) == 1

    def test_clear(self):
        mgr = ConversationManager()
        mgr.append_user_message("C123", "ts1", "hello")
        mgr.clear("C123", "ts1")
        assert mgr.get_messages("C123", "ts1") == []

    def test_thread_safety(self):
        mgr = ConversationManager()
        errors = []

        def writer(thread_id):
            try:
                for i in range(50):
                    mgr.append_user_message("C123", f"ts{thread_id}", f"msg {i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
