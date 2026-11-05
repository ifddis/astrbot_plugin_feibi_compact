import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from astrbot_plugin_feibi_compact.compact_core import CompactError, chunks, estimate, summarize


def history(rounds=12):
    return [{"role": "system", "content": "人格规则"}] + [
        m for i in range(rounds) for m in (
            {"role": "user", "content": f"用户{i}:" + "重要历史内容" * 100},
            {"role": "assistant", "content": f"回复{i}:" + "答复" * 100})]


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_preserves_system_and_tool_rounds_and_bounds_inputs(self):
        original = history()
        original.extend([
            {"role": "user", "content": "最新问题"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "content": "result", "tool_call_id": "x"},
            {"role": "assistant", "content": "最新结果"}])
        snapshot = json.dumps(original)
        requests = []
        async def model(prompt, system):
            requests.append(prompt)
            self.assertLess(len(prompt), 2700)
            return "重要身份、约定和进度。"
        result, calls = await summarize(original, model, keep_rounds=2, chunk_chars=2000,
                                         summary_chars=500, max_calls=100)
        self.assertEqual(snapshot, json.dumps(original))
        self.assertEqual(result[0], original[0])
        self.assertEqual(result[-6:], original[-6:])
        self.assertGreater(calls, 1)
        self.assertLess(estimate(result), estimate(original))

    async def test_second_chunk_failure_never_mutates_original(self):
        original = history()
        snapshot = json.dumps(original)
        calls = 0
        async def model(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("API unavailable")
            return "摘要"
        with self.assertRaises(RuntimeError):
            await summarize(original, model, chunk_chars=1000)
        self.assertEqual(snapshot, json.dumps(original))

    async def test_empty_oversized_and_limit_rejected(self):
        for reply in ('', 'x'*501):
            async def model(*args):
                return reply
            with self.assertRaises(CompactError):
                await summarize(history(), model, summary_chars=500)
        async def model(*args):
            return 'summary'
        with self.assertRaises(CompactError):
            await summarize(history(), model, chunk_chars=1000, max_calls=1)

    async def test_multimedia_payload_not_sent(self):
        messages = [{"role":"user", "content":[{"type":"image_url", "image_url":{"url":"data:image/png;base64,SECRET"}}, {"type":"text", "text":"图片说明"}]}]
        text = ''.join(chunks(messages, 2000))
        self.assertNotIn('SECRET', text)
        self.assertIn('图片说明', text)

    async def test_too_few_rounds_and_embedded_system_rejected(self):
        async def model(*args):
            self.fail('must not call API')
        with self.assertRaises(CompactError):
            await summarize(history(2), model, keep_rounds=6)
        h = history()
        h.insert(3, {"role":"system", "content":"rule"})
        with self.assertRaises(CompactError):
            await summarize(h, model)


# Minimal framework double: production source API contracts were checked against
# AstrBot v4.29.1. These tests do not claim real server integration coverage.
def stub_module(name):
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module

for name in ('astrbot', 'astrbot.api', 'astrbot.api.event', 'astrbot.api.provider',
             'astrbot.api.star', 'astrbot.core', 'astrbot.core.utils',
             'astrbot.core.utils.session_lock'):
    stub_module(name)
sys.modules['astrbot.api'].logger = types.SimpleNamespace(**{
    name: lambda *a, **k: None for name in ('info','error','warning')})
class Filters:
    def __getattr__(self, name):
        return lambda *a, **k: lambda f: f
sys.modules['astrbot.api.event'].filter = Filters()
sys.modules['astrbot.api.event'].AstrMessageEvent = object
sys.modules['astrbot.api.provider'].ProviderRequest = object
class Star:
    def __init__(self, context):
        self.context = context
sys.modules['astrbot.api.star'].Star = Star
sys.modules['astrbot.api.star'].Context = object
data_root = tempfile.TemporaryDirectory()
sys.modules['astrbot.api.star'].StarTools = types.SimpleNamespace(get_data_dir=lambda _: Path(data_root.name))
from contextlib import asynccontextmanager
class LockManager:
    def __init__(self):
        self.lock = asyncio.Lock()
    @asynccontextmanager
    async def acquire_lock(self, key):
        async with self.lock:
            yield
sys.modules['astrbot.core.utils.session_lock'].session_lock_manager = LockManager()
from astrbot_plugin_feibi_compact.main import FeibiCompact

class Event:
    unified_msg_origin = 'qq:GroupMessage:test'
    def __init__(self, admin=True):
        self.admin = admin
        self.extras = {}
    def is_admin(self): return self.admin
    def plain_result(self, text): return text
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value

class Manager:
    def __init__(self):
        self.h = history(20)
        self.writes = 0
    async def get_curr_conversation_id(self, umo): return 'test-cid'
    async def get_conversation(self, umo, cid):
        return types.SimpleNamespace(cid=cid, history=json.dumps(self.h))
    async def update_conversation(self, umo, cid, history):
        self.h = history
        self.writes += 1

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def make(self, provider=None):
        manager = Manager()
        async def text_chat(**kwargs):
            return types.SimpleNamespace(completion_text='伊夫蒂斯的关键约定与进度。')
        async def get_provider(**kwargs):
            return provider or types.SimpleNamespace(text_chat=text_chat)
        ctx = types.SimpleNamespace(conversation_manager=manager,
            get_config=lambda **k:{'agent_runner':{'runner_type':'local'}},
            get_using_provider_async=get_provider)
        plugin = FeibiCompact(ctx, {'auto_threshold_tokens':1000, 'keep_recent_rounds':2})
        return plugin, manager

    async def test_manual_denies_nonadmin(self):
        plugin, manager = self.make()
        output = [s async for s in plugin.compact(Event(False))]
        self.assertIn('管理员', output[0])
        self.assertEqual(manager.writes, 0)

    async def test_manual_shared_lock_blocks_until_chat_done(self):
        plugin, manager = self.make()
        lock = sys.modules['astrbot.core.utils.session_lock'].session_lock_manager
        lock.lock = asyncio.Lock()
        async def run(): return [s async for s in plugin.compact(Event())]
        async with lock.acquire_lock('test'):
            task = asyncio.create_task(run())
            await asyncio.sleep(0.02)
            self.assertEqual(manager.writes, 0)
        output = await task
        self.assertIn('压缩完成', output[-1])
        self.assertEqual(manager.writes, 1)

    async def test_auto_preserves_new_delta_and_persists(self):
        plugin, manager = self.make()
        conv = await manager.get_conversation('', 'test-cid')
        delta = {'role':'user', 'content':'刚刚收到的群聊增量'}
        req = types.SimpleNamespace(conversation=conv, contexts=list(manager.h)+[delta])
        await plugin.auto_compact(Event(), req)
        self.assertEqual(manager.writes, 1)
        self.assertEqual(req.contexts, manager.h+[delta])

    async def test_auto_failure_preserves_db_and_request(self):
        async def fail(**kwargs): raise RuntimeError('client closed')
        plugin, manager = self.make(types.SimpleNamespace(text_chat=fail))
        old = json.dumps(manager.h)
        req = types.SimpleNamespace(conversation=await manager.get_conversation('','test-cid'), contexts=list(manager.h))
        await plugin.auto_compact(Event(), req)
        self.assertEqual(manager.writes, 0)
        self.assertEqual(json.dumps(req.contexts), old)

    async def test_external_history_change_rejects_commit(self):
        plugin, manager = self.make()
        async def race(**kwargs):
            manager.h = manager.h + [{'role':'user','content':'external edit'}]
            return types.SimpleNamespace(completion_text='summary')
        async def get_provider(**kwargs): return types.SimpleNamespace(text_chat=race)
        plugin.context.get_using_provider_async = get_provider
        conv, h = await plugin.current(Event())
        with self.assertRaises(CompactError):
            await plugin.run_compact(Event(), conv, h)
        self.assertEqual(manager.writes, 0)

    async def test_backups_exist_before_write_and_restore_is_scoped(self):
        plugin, manager = self.make()
        conv, h = await plugin.current(Event())
        result, report = await plugin.run_compact(Event(), conv, h)
        filename = report.split('备份：')[-1]
        payload = json.loads((plugin.backups/filename).read_text())
        self.assertEqual(payload['history'], h)
        reply = [s async for s in plugin.restore(Event(), filename)]
        self.assertIn('已恢复', reply[-1])
        self.assertEqual(manager.h, h)
        reply = [s async for s in plugin.restore(Event(), '../secret.json')]
        self.assertIn('用法', reply[-1])

if __name__ == '__main__':
    unittest.main()
