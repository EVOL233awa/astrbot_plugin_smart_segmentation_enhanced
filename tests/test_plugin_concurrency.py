"""插件运行时并发测试（原 test_main_concurrency）。

覆盖 main.py hooks 的会话串行锁与端到端管线，以及
plugin/follow_up（补发任务、锁释放）与 plugin/prepared_store
（内容签名匹配、贪婪拼合回退）的运行时行为。
"""

import asyncio
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.conftest import (  # noqa: E402
    FakeContext,
    FakeEvent,
    FakeLLMResponse,
    FakeResult,
)

from astrbot_plugin_smart_segmentation_enhanced.main import (  # noqa: E402
    _PREPARED_EXTRA_KEY,
    PendingFollowUp,
    SmartSegmentationPlugin,
)


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
        # 每段最多尝试 2 次（首次 + 1 次重试），3 段共 6 次。
        self.assertEqual(len(plugin.context.sent), 6)
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


class TestFollowUpFastForward(unittest.IsolatedAsyncioTestCase):
    """同会话新消息到来时，旧补发剩余分段快速收尾。"""

    def _pending(self, session: str = "s") -> PendingFollowUp:
        return PendingFollowUp(
            session=session,
            segments=["第三句内容。", "第四句内容。"],
            delay_base=5.0,
            delay_per_char=0.0,
            delay_max=5.0,
            expires_at=999999999.0,
        )

    def test_fast_forward_marks_only_target_session(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        same = self._pending("s")
        other = self._pending("other")
        running = self._pending("s")
        plugin._pending_follow_ups["a"] = same
        plugin._pending_follow_ups["b"] = other
        plugin._running_follow_ups["s"] = running

        affected = plugin._fast_forward_follow_ups("s")

        self.assertEqual(affected, 2)
        self.assertTrue(same.fast_forward)
        self.assertTrue(running.fast_forward)
        self.assertFalse(other.fast_forward)

    async def test_fast_forward_sends_remaining_segments_quickly(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        pending = self._pending()
        plugin._pending_follow_ups["a"] = pending
        plugin._fast_forward_follow_ups("s")

        started = time.monotonic()
        await plugin._run_follow_up_segments(pending)
        elapsed = time.monotonic() - started

        self.assertEqual(len(plugin.context.sent), 2)
        self.assertLess(elapsed, 1.5)

    async def test_new_message_triggers_fast_forward(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        pending = self._pending()
        plugin._pending_follow_ups["a"] = pending

        text = "新一轮回复的内容足够长，用来触发预处理。"
        await plugin.on_llm_response(
            FakeEvent("s", FakeResult(text)),
            FakeLLMResponse(text),
        )

        self.assertTrue(pending.fast_forward)


class TestPlatformGuard(unittest.IsolatedAsyncioTestCase):
    """不支持主动发送的平台整条发出，避免只发首段。"""

    async def test_official_platform_sends_whole_reply(self):
        class PlatformEvent(FakeEvent):
            def get_platform_name(self):
                return "qq_official_webhook"

        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        text = "第一句内容比较长，用于测试平台守卫。第二句内容也很长。"
        event = PlatformEvent("s", FakeResult(text))

        await plugin.on_decorating_result(event)

        self.assertEqual(event.result.chain[0].text, text)
        self.assertNotIn("smart_segmentation_pending_id", event.extra)


class TestFollowUpTimeout(unittest.IsolatedAsyncioTestCase):
    """发送超时视为不确定投递，不再重试，避免重复气泡。"""

    async def test_timeout_is_not_retried(self):
        from astrbot_plugin_smart_segmentation_enhanced.plugin import follow_up as follow_up_mod

        class HangingContext(FakeContext):
            def __init__(self):
                super().__init__()
                self.calls = 0

            async def send_message(self, session, chain):
                self.calls += 1
                await asyncio.sleep(0.5)
                self.sent.append((session, chain))
                return True

        plugin = SmartSegmentationPlugin(HangingContext(), {"enabled": True})
        pending = PendingFollowUp(
            session="s",
            segments=["seg1"],
            delay_base=0.0,
            delay_per_char=0.0,
            delay_max=0.0,
            expires_at=999999999.0,
        )
        original_timeout = follow_up_mod._FOLLOW_UP_SEND_TIMEOUT_SECONDS
        follow_up_mod._FOLLOW_UP_SEND_TIMEOUT_SECONDS = 0.05
        try:
            await plugin._run_follow_up_segments(pending)
        finally:
            follow_up_mod._FOLLOW_UP_SEND_TIMEOUT_SECONDS = original_timeout

        self.assertEqual(plugin.context.calls, 1)


class TestFastForwardToggle(unittest.IsolatedAsyncioTestCase):
    """加速收尾的开关与间隔来自面板配置。"""

    def _pending(self) -> PendingFollowUp:
        return PendingFollowUp(
            session="s",
            segments=["第三句内容。"],
            delay_base=5.0,
            delay_per_char=0.0,
            delay_max=5.0,
            expires_at=999999999.0,
        )

    async def test_new_message_does_not_fast_forward_when_disabled(self):
        plugin = SmartSegmentationPlugin(
            FakeContext(),
            {"enabled": True, "fast_forward_enabled": False},
        )
        pending = self._pending()
        plugin._pending_follow_ups["a"] = pending

        text = "新一轮回复的内容足够长，用来触发预处理。"
        await plugin.on_llm_response(
            FakeEvent("s", FakeResult(text)),
            FakeLLMResponse(text),
        )

        self.assertFalse(pending.fast_forward)

    def test_register_snapshots_configured_delay(self):
        plugin = SmartSegmentationPlugin(
            FakeContext(),
            {"enabled": True, "fast_forward_delay": 0.4},
        )
        settings = plugin._get_settings()
        pending_id = plugin._register_pending_follow_up(
            session="s",
            segments=["第一段。"],
            settings=settings,
        )
        pending = plugin._pending_follow_ups[pending_id]
        self.assertAlmostEqual(pending.fast_forward_delay_seconds, 0.4)


class TestFastForwardRegistrationRace(unittest.IsolatedAsyncioTestCase):
    """新消息的 on_llm_response 早于旧回复登记补发时，旧队列也要加速。"""

    TEXT = "第一句内容比较长，用于测试新消息先到的场景。" * 8

    async def _decorate_and_get_pending(self, plugin, event):
        await plugin.on_decorating_result(event)
        pending_id = event.extra["smart_segmentation_pending_id"]
        return plugin._pending_follow_ups[pending_id]

    async def test_newer_message_marks_existing_reply_fast_forward(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        event_a = FakeEvent("s", FakeResult(self.TEXT))
        event_b = FakeEvent("s", FakeResult(self.TEXT))

        await plugin.on_llm_response(event_a, FakeLLMResponse(self.TEXT))
        await plugin.on_llm_response(event_b, FakeLLMResponse(self.TEXT))
        pending = await self._decorate_and_get_pending(plugin, event_a)

        self.assertTrue(pending.fast_forward)
        await plugin.terminate()

    async def test_single_message_does_not_fast_forward(self):
        plugin = SmartSegmentationPlugin(FakeContext(), {"enabled": True})
        event = FakeEvent("s", FakeResult(self.TEXT))

        await plugin.on_llm_response(event, FakeLLMResponse(self.TEXT))
        pending = await self._decorate_and_get_pending(plugin, event)

        self.assertFalse(pending.fast_forward)
        await plugin.terminate()


if __name__ == "__main__":
    unittest.main()
