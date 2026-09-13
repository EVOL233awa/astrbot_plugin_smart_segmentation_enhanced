"""共享测试基建：AstrBot 框架 fake 注入与通用桩件。

pytest 在收集测试模块前自动加载本文件，任何测试模块在被收集前
必须已完成 astrbot.* 假模块注入，`import astrbot_plugin_smart_segmentation_enhanced.main`
才能脱离 AstrBot 宿主环境独立运行。
"""

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PARENT = os.path.dirname(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, PLUGIN_PARENT)


def _install_fake_astrbot():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_mod = types.ModuleType("astrbot.api.event")
    filter_mod = types.ModuleType("astrbot.api.event.filter")
    components = types.ModuleType("astrbot.api.message_components")
    provider = types.ModuleType("astrbot.api.provider")
    star = types.ModuleType("astrbot.api.star")

    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.event"] = event_mod
    sys.modules["astrbot.api.event.filter"] = filter_mod
    sys.modules["astrbot.api.message_components"] = components
    sys.modules["astrbot.api.provider"] = provider
    sys.modules["astrbot.api.star"] = star

    class AstrBotConfig(dict):
        pass

    class Logger:
        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

        def debug(self, *args, **kwargs):
            pass

    api.AstrBotConfig = AstrBotConfig
    api.logger = Logger()

    class AstrMessageEvent:
        pass

    class MessageChain:
        def __init__(self, chain):
            self.chain = chain

    class MessageEventResult:
        pass

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.MessageChain = MessageChain
    event_mod.MessageEventResult = MessageEventResult

    def hook(*args, **kwargs):
        def deco(fn):
            return fn

        return deco

    filter_mod.on_llm_response = hook
    filter_mod.on_decorating_result = hook
    filter_mod.after_message_sent = hook
    event_mod.filter = filter_mod

    class Plain:
        def __init__(self, text):
            self.text = text

    components.Plain = Plain

    class LLMResponse:
        pass

    provider.LLMResponse = LLMResponse

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context

    star.Context = Context
    star.Star = Star

    return AstrBotConfig, MessageChain, Plain, Context, Star


AstrBotConfig, MessageChain, Plain, Context, Star = _install_fake_astrbot()


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
