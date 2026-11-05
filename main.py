"""AstrBot 4.29.1 compact command and request-time compaction."""
import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.utils.session_lock import session_lock_manager

from .compact_core import CompactError, estimate, summarize


class FeibiCompact(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir("astrbot_plugin_feibi_compact"))
        self.backups = self.data_dir / "backups"
        self.backups.mkdir(parents=True, exist_ok=True)
        self.backups.chmod(0o700)
        self.gate = asyncio.Lock()  # Only one summary chain across all sessions.
        self.last_attempt = {}
        self.active_tasks = set()
        self.stopping = False

    def number(self, key, default, low, high):
        return max(low, min(high, int(self.config.get(key, default))))

    def allowed(self, event):
        entries = self.config.get("allowed_sessions", [])
        return not entries or event.unified_msg_origin in entries

    def local_runner(self, event):
        runner = self.context.get_config(umo=event.unified_msg_origin).get("agent_runner", {})
        return isinstance(runner, dict) and runner.get("runner_type", "local") == "local"

    async def current(self, event):
        manager = self.context.conversation_manager
        cid = await manager.get_curr_conversation_id(event.unified_msg_origin)
        conv = await manager.get_conversation(event.unified_msg_origin, cid) if cid else None
        if not conv:
            raise CompactError("当前没有对话。")
        history = json.loads(conv.history) if isinstance(conv.history, str) else conv.history
        if not isinstance(history, list) or not all(isinstance(m, dict) for m in history):
            raise CompactError("当前历史格式不受支持。")
        return conv, history

    async def provider(self, event):
        pid = str(self.config.get("summary_provider_id", "")).strip()
        if not pid:
            pid = str(self.context.get_config(umo=event.unified_msg_origin).get(
                "provider_settings", {}).get("llm_compress_provider_id", "")).strip()
        provider = self.context.get_provider_by_id(pid) if pid else await self.context.get_using_provider_async(
            umo=event.unified_msg_origin)
        if not provider or not callable(getattr(provider, "text_chat", None)):
            raise CompactError("摘要对话模型不可用；请填写对话 Provider ID，不能使用 Embedding。")
        return provider

    def write_backup(self, umo, cid, history):
        tag = hashlib.sha256((umo + ':' + cid).encode()).hexdigest()[:20]
        name = f"{tag}-{time.time_ns()}-{uuid.uuid4().hex[:6]}.json"
        target = self.backups / name
        temporary = target.with_suffix(".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"umo": umo, "cid": cid, "created_at": time.time(),
                           "history": history}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def trim_backups(self):
        # Bound total retained snapshots, including failed compaction attempts.
        keep = self.number("backup_count", 20, 2, 200)
        for path in sorted(self.backups.glob("*.json"), reverse=True,
                           key=lambda p: p.stat().st_mtime_ns)[keep:]:
            path.unlink(missing_ok=True)

    async def run_compact(self, event, conv, history):
        if self.stopping:
            raise CompactError("插件正在停止。")
        if self.gate.locked():
            raise CompactError("已有压缩任务执行中，请稍后再试。")
        async with self.gate:
            task = asyncio.current_task()
            self.active_tasks.add(task)
            try:
                provider = await self.provider(event)  # Resolve afresh, no stale client cache.
                backup = await asyncio.to_thread(self.write_backup, event.unified_msg_origin,
                                                  conv.cid, history)
                await asyncio.to_thread(self.trim_backups)
                before = estimate(history)
                logger.info(f"[FeibiCompact] start cid={conv.cid} estimated_tokens={before} backup={backup.name}")

                async def call(prompt, system):
                    async with asyncio.timeout(self.number("request_timeout", 120, 10, 600)):
                        response = await provider.text_chat(
                            prompt=prompt, contexts=[], system_prompt=system,
                            request_max_retries=1,
                            max_tokens=self.number("summary_max_tokens", 3000, 256, 16000),
                        )
                    return response.completion_text

                async with asyncio.timeout(self.number("total_timeout", 1800, 30, 7200)):
                    result, calls = await summarize(
                        history, call,
                        keep_rounds=self.number("keep_recent_rounds", 6, 1, 100),
                        chunk_chars=self.number("chunk_chars", 24000, 2000, 200000),
                        summary_chars=self.number("summary_max_chars", 6000, 500, 30000),
                        max_calls=self.number("max_summary_calls", 100, 1, 500),
                        progress=lambda n: logger.info(f"[FeibiCompact] cid={conv.cid} summarized_chunk={n}"),
                    )
                # The caller holds AstrBot's own session lock. Also reject external edits.
                current, latest = await self.current(event)
                if current.cid != conv.cid or latest != history:
                    raise CompactError("总结期间历史或对话已改变，放弃写入；原始备份保留。")
                await self.context.conversation_manager.update_conversation(
                    event.unified_msg_origin, conv.cid, history=result)
                try:
                    await asyncio.to_thread(self.trim_backups)
                except OSError:
                    logger.warning("[FeibiCompact] 旧备份清理失败；压缩已保存。")
                after = estimate(result)
                logger.info(f"[FeibiCompact] committed cid={conv.cid} estimated_tokens={before}->{after} calls={calls}")
                return result, f"压缩完成：估算 {before:,} → {after:,} Token；{calls} 次摘要调用。\n备份：{backup.name}"
            finally:
                self.active_tasks.discard(task)

    @filter.command("compact")
    async def compact(self, event: AstrMessageEvent):
        """管理员立即压缩当前对话。"""
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员可以压缩当前上下文。")
            return
        if not self.allowed(event) or not self.local_runner(event):
            yield event.plain_result("当前会话未授权，或没有使用 AstrBot 内置 AI。")
            return
        if self.gate.locked():
            yield event.plain_result("已有压缩正在执行，请勿重复触发。")
            return
        yield event.plain_result("正在等待当前回复结束并压缩；超长历史会分块总结，请勿重复发送。")
        try:
            async with session_lock_manager.acquire_lock(event.unified_msg_origin):
                conv, history = await self.current(event)
                _, report = await self.run_compact(event, conv, history)
            yield event.plain_result(report)
        except Exception as exc:
            logger.error(f"[FeibiCompact] manual failed: {exc}", exc_info=True)
            yield event.plain_result(f"压缩未完成：{type(exc).__name__}: {exc}\n失败时不写入半成品；请查看日志。")

    @filter.command("compact_status")
    async def status(self, event: AstrMessageEvent):
        """管理员查看当前历史估算和插件阈值。"""
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员可以查看压缩状态。")
            return
        try:
            async with session_lock_manager.acquire_lock(event.unified_msg_origin):
                conv, history = await self.current(event)
                tokens = estimate(history)
            yield event.plain_result(
                f"当前对话：{conv.cid}\n历史消息：{len(history)}\n"
                f"历史估算 Token：{tokens:,}（非模型精确计数）\n"
                f"自动触发阈值：{self.number('auto_threshold_tokens', 120000, 1000, 10000000):,}\n"
                f"自动压缩：{bool(self.config.get('auto_enabled', True))}\n"
                f"保留最近轮数：{self.number('keep_recent_rounds', 6, 1, 100)}\n"
                f"全局压缩执行中：{self.gate.locked()}")
        except Exception as exc:
            yield event.plain_result(f"无法读取状态：{exc}")

    @filter.on_llm_request(priority=-10000)
    async def auto_compact(self, event: AstrMessageEvent, req: ProviderRequest):
        # AstrBot 4.29.1 calls this hook inside the shared session lock.
        if (self.stopping or not self.config.get("auto_enabled", True)
                or not self.allowed(event) or not self.local_runner(event)
                or not req.conversation or self.gate.locked()):
            return
        if event.get_extra("feibi_compact_checked", False):
            return
        event.set_extra("feibi_compact_checked", True)
        try:
            conv, history = await self.current(event)
            if conv.cid != req.conversation.cid:
                return
            # Other plugins may append fresh group deltas. Preserve them, and refuse
            # transformations that cannot be aligned with the persisted prefix.
            if not isinstance(req.contexts, list) or req.contexts[:len(history)] != history:
                logger.warning("[FeibiCompact] request history differs from DB prefix; skipping auto compression")
                return
            threshold = self.number("auto_threshold_tokens", 120000, 1000, 10000000)
            if estimate(history) < threshold:
                return
            now = time.monotonic()
            if now - self.last_attempt.get(conv.cid, -1e9) < self.number("cooldown_seconds", 300, 30, 86400):
                return
            if len(self.last_attempt) >= 1024:
                self.last_attempt.pop(next(iter(self.last_attempt)))
            self.last_attempt[conv.cid] = now
            result, report = await self.run_compact(event, conv, history)
            req.contexts = result + req.contexts[len(history):]
            req.conversation.history = json.dumps(result, ensure_ascii=False)
            logger.info(f"[FeibiCompact] auto {report}")
        except Exception as exc:
            logger.error(f"[FeibiCompact] auto failed; original request retained: {exc}", exc_info=True)

    @filter.command("compact_restore")
    async def restore(self, event: AstrMessageEvent, filename: str = ""):
        """管理员按压缩报告中的文件名恢复当前对话。恢复会替换整个历史。"""
        if not event.is_admin():
            yield event.plain_result("只有 AstrBot 管理员可以恢复备份。")
            return
        if not filename or Path(filename).name != filename or not filename.endswith(".json"):
            yield event.plain_result("用法：/compact_restore 压缩报告中的备份文件名.json；会替换当前历史。")
            return
        if not self.allowed(event) or not self.local_runner(event):
            yield event.plain_result("当前会话不允许恢复。")
            return
        try:
            async with session_lock_manager.acquire_lock(event.unified_msg_origin):
                conv, history = await self.current(event)
                path = self.backups / filename
                if path.is_symlink():
                    raise CompactError("不允许符号链接。")
                payload = await asyncio.to_thread(lambda: json.loads(path.read_text(encoding="utf-8")))
                if payload["umo"] != event.unified_msg_origin or payload["cid"] != conv.cid:
                    raise CompactError("只能恢复当前会话、当前对话的备份。")
                saved = await asyncio.to_thread(self.write_backup, event.unified_msg_origin, conv.cid, history)
                await self.context.conversation_manager.update_conversation(
                    event.unified_msg_origin, conv.cid, history=payload["history"])
            yield event.plain_result(f"已恢复。恢复前历史另存为：{saved.name}；恢复点之后的聊天不在恢复内容中。")
        except Exception as exc:
            yield event.plain_result(f"恢复失败：{exc}")

    async def terminate(self):
        self.stopping = True
        tasks = [t for t in self.active_tasks if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
