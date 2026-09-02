"""Concurrency tests for main.py session serialization."""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import (  # noqa: E402,F401
    FakeContext,
    FakeEvent,
    FakeLLMResponse,
    FakeResult,
    MessageChain,
    Plain,
)

from astrbot_plugin_smart_segmentation.main import (  # noqa: E402
    _PREPARED_EXTRA_KEY,
    PendingFollowUp,
    SmartSegmentationPlugin,
)


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, session, chain):
        self.sent.append((session, chain))
        return True


class FakeResult:
    def __init__(self, text):
        self.chain = [Plain(text)]
        self._model_result = True

    def is_model_result(self):
        return self._model_result


class FakeLLMResponse:
    def __init__(self, text):
        self.role = "assistant"
        self.result_chain = MessageChain([Plain(text)])
        self.completion_text = text


class FakeEvent:
    def __init__(self, session, result):
        self.unified_msg_origin = session
        self.result = result
        self.extra = {}

    def get_result(self):
        return self.result

    def set_extra(self, key, value):
        self.extra[key] = value

    def get_extra(self, key, default=""):
        return self.extra.get(key, default)


class TestSessionSerialization(unittest.IsolatedAsyncioTestCase):
    def _make_plugin(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        first = "第一句内容比较长，用于测试串行锁。" * 8
        second = "第二句内容也比较长，用于确认补发。" * 8
        plugin._store_prepared_segments(
            "s",
            first + second,
            [first, second],
        )
        return plugin

    async def test_decorate_waits_for_active_session_lock(self):
        plugin = self._make_plugin()
        lock = asyncio.Lock()
        await lock.acquire()
        plugin._session_locks["s"] = lock

        first = "第一句内容比较长，用于测试串行锁。" * 8
        second = "第二句内容也比较长，用于确认补发。" * 8
        event = FakeEvent("s", FakeResult(first + second))
        task = asyncio.create_task(plugin.on_decorating_result(event))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())

        lock.release()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(event.result.chain[0].text, first)
        pending_id = event.extra["smart_segmentation_pending_id"]
        pending = plugin._pending_follow_ups.get(pending_id)
        self.assertIsNotNone(pending)
        self.assertIs(pending.lock, lock)

    async def test_follow_up_task_releases_lock(self):
        plugin = self._make_plugin()
        lock = asyncio.Lock()
        await lock.acquire()
        pending = PendingFollowUp(
            session="s",
            segments=["第二句。"],
            delay_base=0.0,
            delay_per_char=0.0,
            delay_max=0.0,
            expires_at=999999999.0,
            lock=lock,
        )

        await plugin._run_follow_up_segments(pending)

        self.assertFalse(lock.locked())
        self.assertEqual(len(plugin.context.sent), 1)
        self.assertIsNone(pending.lock)

    def test_content_signature_exact_match(self):
        plugin = self._make_plugin()
        plugin._store_prepared_segments(
            "s",
            "今天天气很好。",
            ["今天天气很好。"],
        )
        self.assertEqual(
            plugin._pop_prepared_segments(
                "s",
                "今天天气很好，",
                min_length=15,
                max_segments=8,
            ),
            ["今天天气很好。"],
        )

    def test_greedy_assembly_for_tool_loop(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        plugin._store_prepared_segments("s", "第一段。", ["第一段。"])
        plugin._store_prepared_segments("s", "第二段。", ["第二段。"])
        self.assertEqual(
            plugin._pop_prepared_segments(
                "s",
                "第一段。第二段。",
                min_length=15,
                max_segments=8,
            ),
            ["第一段。", "第二段。"],
        )

    async def test_full_pipeline_local_only(self):
        plugin = SmartSegmentationPlugin(
            FakeContext(),
            {
                "enabled": True,
                "use_local_only": True,
                "delay_base": 0.0,
                "delay_per_char": 0.0,
                "delay_max": 0.0,
            },
        )
        text = (
            "（开心）"
            "今天需要测试的内容足够长，不能因为目标长度被合并成一条。"
            "第二句也足够长，用来验证补发。"
            "第三句继续补充内容，确保本地切分产生多条消息。"
        )
        event = FakeEvent("s", FakeResult(text))
        response = FakeLLMResponse(text)

        await plugin.on_llm_response(event, response)
        self.assertTrue(plugin._prepared_segments)

        await plugin.on_decorating_result(event)
        self.assertIn("smart_segmentation_pending_id", event.extra)
        self.assertNotEqual(event.result.chain[0].text, text)

        await plugin.after_message_sent(event)
        tasks = list(plugin._active_follow_up_tasks)
        await asyncio.gather(*tasks, return_exceptions=True)

        self.assertGreaterEqual(len(plugin.context.sent), 1)
        self.assertFalse(plugin._session_locks["s"].locked())

    async def test_local_only_json_passes_through(self):
        plugin = SmartSegmentationPlugin(
            FakeContext(),
            {"enabled": True, "use_local_only": True},
        )
        json_text = '{"key": "value with braces {x}", "arr": [1, 2]}'
        event = FakeEvent("s", FakeResult(json_text))
        response = FakeLLMResponse(json_text)

        await plugin.on_llm_response(event, response)
        await plugin.on_decorating_result(event)

        self.assertEqual(event.result.chain[0].text, json_text)
        self.assertNotIn("smart_segmentation_pending_id", event.extra)

    async def test_local_only_code_fence_protected(self):
        plugin = SmartSegmentationPlugin(
            FakeContext(),
            {
                "enabled": True,
                "use_local_only": True,
                "delay_base": 0.0,
                "delay_per_char": 0.0,
                "delay_max": 0.0,
            },
        )
        text = (
            "Intro text.\n\n"
            "```python\nprint('a')\n\nprint('b')\n```\n\n"
            "Done."
        )
        event = FakeEvent("s", FakeResult(text))
        response = FakeLLMResponse(text)

        await plugin.on_llm_response(event, response)
        await plugin.on_decorating_result(event)

        self.assertEqual(event.result.chain[0].text, "Intro text.")
        pending_id = event.extra["smart_segmentation_pending_id"]
        pending = plugin._pending_follow_ups[pending_id]
        self.assertTrue(
            any(
                seg.startswith("```python") and seg.endswith("```")
                for seg in pending.segments
            )
        )

    async def test_protected_block_uses_local_without_provider(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        text = "配置如下：\n\n{\n  \"host\": \"localhost\"\n}\n\n请检查。"
        event = FakeEvent("s", FakeResult(text))
        response = FakeLLMResponse(text)

        await plugin.on_llm_response(event, response)

        self.assertTrue(plugin._prepared_segments)

    async def test_expired_pending_lock_released_before_next_decorate(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        text = (
            "first long segment enough for testing lock. "
            "second long segment enough for blocking."
        )
        plugin._store_prepared_segments(
            "s",
            text,
            [
                "first long segment enough for testing lock.",
                "second long segment enough for blocking.",
            ],
        )
        ev1 = FakeEvent("s", FakeResult(text))
        await plugin.on_decorating_result(ev1)
        pending_id = ev1.extra["smart_segmentation_pending_id"]
        plugin._pending_follow_ups[pending_id].expires_at = time.monotonic() - 1

        ev2 = FakeEvent("s", FakeResult(text))
        task = asyncio.create_task(plugin.on_decorating_result(ev2))
        await asyncio.wait_for(task, timeout=1)
        self.assertIn("smart_segmentation_pending_id", ev2.extra)

    async def test_follow_up_retries_failed_send(self):
        class RetryContext(FakeContext):
            def __init__(self):
                super().__init__()
                self.fail_first = True

            async def send_message(self, session, chain):
                if self.fail_first:
                    self.fail_first = False
                    return False
                self.sent.append((session, chain))
                return True

        plugin = SmartSegmentationPlugin(RetryContext(), {"enabled": True})
        lock = asyncio.Lock()
        await lock.acquire()
        pending = PendingFollowUp(
            session="s",
            segments=["seg1", "seg2", "seg3"],
            delay_base=0.0,
            delay_per_char=0.0,
            delay_max=0.0,
            expires_at=999999999.0,
            lock=lock,
        )
        await plugin._run_follow_up_segments(pending)
        self.assertEqual(len(plugin.context.sent), 3)
        self.assertFalse(lock.locked())

    async def test_follow_up_continues_after_permanent_failure(self):
        class AlwaysFailContext(FakeContext):
            async def send_message(self, session, chain):
                self.sent.append((session, chain))
                return False

        plugin = SmartSegmentationPlugin(AlwaysFailContext(), {"enabled": True})
        lock = asyncio.Lock()
        await lock.acquire()
        pending = PendingFollowUp(
            session="s",
            segments=["seg1", "seg2", "seg3"],
            delay_base=0.0,
            delay_per_char=0.0,
            delay_max=0.0,
            expires_at=999999999.0,
            lock=lock,
        )
        await plugin._run_follow_up_segments(pending)
        self.assertEqual(len(plugin.context.sent), 9)
        self.assertFalse(lock.locked())

    async def test_event_extra_binding_avoids_cache_fallback(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        original = "原始回复内容足够长，用于验证事件绑定。"
        prepared = ["自定义边界一", "自定义边界二", "自定义边界三"]
        suffix = "其他插件追加的尾部内容。"
        outbound = original + suffix
        event = FakeEvent("s", FakeResult(outbound))
        event.set_extra(
            _PREPARED_EXTRA_KEY,
            {"text": original, "segments": list(prepared)},
        )
        await plugin.on_decorating_result(event)
        pending_id = event.extra["smart_segmentation_pending_id"]
        pending = plugin._pending_follow_ups[pending_id]
        self.assertEqual(event.result.chain[0].text, prepared[0])
        self.assertTrue(pending.segments[:2] == prepared[1:])
        self.assertTrue(any(suffix[:8] in seg for seg in pending.segments))


if __name__ == "__main__":
    unittest.main()
