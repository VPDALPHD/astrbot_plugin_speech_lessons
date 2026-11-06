"""教我说话 —— 在私聊里被纠正过一次，它就记住这个说法。

设计原则：
1. 只有主人能在私聊里教（群里教一律不学）；
2. 判定「是不是在教」走三档：强信号直接学、模糊交给模型、闲聊不烧钱；
3. 规则写进 system_prompt 的受信任位，比记忆条目更有约束力；
4. 复习推进只认主人的明确反馈，不做「注入即复习成功」的伪复习。
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import judge as judge_mod
from .store import LessonStore, Rule

PLUGIN_NAME = "astrbot_plugin_speech_lessons"
INJECT_MARKER = "<!--speech_lessons:rules-->"
MAX_TASK_KEEP = 32


class SpeechLessonsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config  # type: ignore[assignment]

        data_dir = self._resolve_data_dir()
        self.store = LessonStore(os.path.join(data_dir, "lessons.db"))

        self._runtime_enabled = True
        self._inject_ids_by_session: dict[str, list[int]] = {}
        self._pending_notes: dict[str, str] = {}
        self._judge_tasks: set[asyncio.Task] = set()
        self._maint_task: asyncio.Task | None = None
        self._last_decay_at = 0.0

        logger.info(
            "[speech_lessons] 已加载，规则数=%d，数据目录=%s",
            self.store.count_active(),
            data_dir,
        )

    # ================================================================== 启动
    def _resolve_data_dir(self) -> str:
        try:
            path = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(path, exist_ok=True)
        return path

    async def initialize(self):
        if self._maint_task is None or self._maint_task.done():
            self._maint_task = asyncio.create_task(self._maintenance_loop())

    async def terminate(self):
        for task in (self._maint_task, *self._judge_tasks):
            if task and not task.done():
                task.cancel()
        self.store.close()

    async def _maintenance_loop(self):
        while True:
            try:
                await asyncio.sleep(3600)
                self._run_idle_decay()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("[speech_lessons] 维护任务异常: %s", exc)

    def _run_idle_decay(self):
        if time.time() - self._last_decay_at < 1800:
            return
        self._last_decay_at = time.time()
        try:
            archived = self.store.apply_idle_decay(
                per_day=float(self._cfg("decay_per_day", 0.02)),
                archive_weight=float(self._cfg("archive_weight", 0.3)),
            )
            if archived:
                logger.info("[speech_lessons] 闲置衰减归档 %d 条", archived)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[speech_lessons] 衰减失败: %s", exc)

    # ================================================================== 配置
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    def _owner_ids(self) -> set[str]:
        raw = str(self._cfg("owner_ids", "") or "")
        for sep in ("，", "；", ";", " ", "\n"):
            raw = raw.replace(sep, ",")
        return {item.strip() for item in raw.split(",") if item.strip()}

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            return False
        ids = self._owner_ids()
        if ids:
            return sender in ids
        # owner_ids 没配：只认平台管理员，绝不默认放行任何人
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True)) and self._runtime_enabled

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        try:
            return str(event.unified_msg_origin or "")
        except Exception:
            return ""

    @staticmethod
    def _is_command(text: str) -> bool:
        t = (text or "").strip()
        return t.startswith("/") or t.startswith("!") or t.startswith("。")

    # ================================================================== 捕获
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def capture_lesson(self, event: AstrMessageEvent):
        """私聊里识别「在教」的消息。只有主人说话才算。"""
        if not self._enabled() or not bool(self._cfg("capture_enabled", True)):
            return
        if not self._is_owner(event):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or self._is_command(text) or len(text) > 500:
            return
        try:
            if event.is_stopped():
                return
        except Exception:
            pass

        key = self._session_key(event)

        # 1) 先看是不是对我上一条回复的评价
        feedback = judge_mod.detect_feedback(text)
        if feedback is not None:
            ids = self._inject_ids_by_session.get(key) or []
            if ids:
                changed = self.store.feedback(ids, positive=feedback)
                if changed:
                    logger.info(
                        "[speech_lessons] 反馈(%s) 命中 %d 条规则",
                        "赞" if feedback else "否",
                        changed,
                    )
                if not feedback:
                    self._pending_notes[key] = "主人刚说你刚才那条又不对了，这轮先按他的意思改，不要辩解。"
                return

        # 2) 判定是不是在教
        level = judge_mod.detect(text)
        if level == "chat":
            return

        if level == "strong":
            items = judge_mod.extract_by_rules(text)
            if items:
                self._store_items(items, text)
                return
            # 抽不出来就降级给模型
            level = "possible"

        if level == "possible" and bool(self._cfg("judge_enabled", True)):
            if len(self._judge_tasks) > MAX_TASK_KEEP:
                logger.debug("[speech_lessons] 判定任务过多，跳过本轮")
                return
            task = asyncio.create_task(self._judge_and_store(text, key))
            self._judge_tasks.add(task)
            task.add_done_callback(self._judge_tasks.discard)

    async def _judge_and_store(self, text: str, umo: str):
        provider_id = str(self._cfg("judge_provider_id", "") or "")
        if not provider_id:
            try:
                provider_id = await self.context.get_current_chat_provider_id(umo=umo)
            except Exception:
                provider_id = ""
        if not provider_id:
            logger.debug("[speech_lessons] 拿不到 provider，跳过模型判定")
            return
        timeout = float(self._cfg("judge_timeout_seconds", 8.0))
        data = await judge_mod.judge_with_model(
            self.context, provider_id, text, timeout=timeout
        )
        if not data:
            return
        min_conf = float(self._cfg("min_confidence", 0.5))
        if float(data.get("confidence") or 0.0) < min_conf:
            logger.info(
                "[speech_lessons] 模型判定置信度不足(%.2f)，不学: %s",
                data.get("confidence") or 0.0,
                data.get("rule"),
            )
            return
        self._store_items([data], text)

    def _store_items(self, items: list[dict[str, Any]], raw_text: str):
        max_rules = int(self._cfg("max_rules", 300))
        if self.store.count_active() >= max_rules:
            logger.warning("[speech_lessons] 规则数已达上限 %d，忽略本次教学", max_rules)
            return
        max_chars = int(self._cfg("max_rule_chars", 200))
        scene = str(self._cfg("default_scene", "all") or "all")
        stored = 0
        for item in items:
            rule = str(item.get("rule") or "").strip()[:max_chars]
            if len(rule) < 2:
                continue
            wrong = str(item.get("wrong") or "").strip()[:max_chars]
            tags = item.get("tags") or []
            self.store.add(
                rule=rule,
                wrong=wrong,
                scene=scene,
                tags=tags,
                weight=float(self._cfg("new_rule_weight", 1.5)),
                source=str(item.get("source") or "chat"),
                raw_text=raw_text[:500],
            )
            stored += 1
        if stored:
            logger.info("[speech_lessons] 学到 %d 条新规则", stored)

    # ================================================================== 注入
    @filter.on_llm_request(priority=-100)
    async def inject_rules(self, event: AstrMessageEvent, req: Any):
        if not self._enabled() or not bool(self._cfg("inject_enabled", True)):
            return
        text = str(getattr(event, "message_str", "") or "")
        if self._is_command(text):
            return
        try:
            rules = self.store.active(scene="all", limit=int(self._cfg("scan_rules", 200)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[speech_lessons] 读取规则失败: %s", exc)
            return
        if not rules:
            return

        limit = int(self._cfg("inject_max_rules", 8))
        budget = int(self._cfg("inject_max_chars", 700))
        # 到复习期的规矩优先露脸（这才是"复习"），其余按权重排
        now = time.time()
        rules.sort(
            key=lambda r: (
                0 if (r.next_review_at and r.next_review_at <= now) else 1,
                -r.weight,
            )
        )
        picked: list[Rule] = []
        used = 0
        for rule in rules:
            line_len = len(rule.rule) + (len(rule.wrong) + 6 if rule.wrong else 0) + 4
            if picked and used + line_len > budget:
                break
            picked.append(rule)
            used += line_len
            if len(picked) >= limit:
                break

        lines = []
        for rule in picked:
            if rule.wrong:
                lines.append(f"- 不要说「{rule.wrong}」，{rule.rule}")
            else:
                lines.append(f"- {rule.rule}")
        block = (
            f"\n\n{INJECT_MARKER}\n"
            "【说话规矩 · 主人在私聊里亲自教的，优先级高于一般风格偏好】\n"
            + "\n".join(lines)
            + "\n（自然遵守就好，不要背诵或复述这些规则本身。）"
        )

        note = self._pending_notes.pop(self._session_key(event), "")
        if note and bool(self._cfg("confirm_note", True)):
            block += f"\n【本轮附注】{note}"

        try:
            req.system_prompt = (getattr(req, "system_prompt", "") or "") + block
        except Exception as exc:  # noqa: BLE001
            logger.warning("[speech_lessons] 注入失败: %s", exc)
            return

        self._inject_ids_by_session[self._session_key(event)] = [r.id for r in picked]
        try:
            self.store.touch([r.id for r in picked])
        except Exception:
            pass

    # ================================================================== 指令
    @filter.command("教")
    async def cmd_teach(self, event: AstrMessageEvent):
        """教我说话：/教 列表 · /教 删 3 · /教 清 · /教 关 · /教 开 · /教 测试 <话>"""
        if not self._is_owner(event):
            yield event.plain_result("这个只有主人能用。")
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        args = raw.split()[1:]
        if not args:
            yield event.plain_result(
                "用法：\n"
                "/教 列表 —— 看我记住了哪些说话规矩\n"
                "/教 删 3 —— 删掉第 3 条\n"
                "/教 清 —— 清空全部\n"
                "/教 关 / 开 —— 临时停用/恢复\n"
                "/教 测试 <一句话> —— 看这句话会不会被当成教学"
            )
            return

        action = args[0]
        if action in ("列表", "list"):
            rules = self.store.active(limit=50)
            if not rules:
                yield event.plain_result("还没有记住任何说话规矩。")
                return
            lines = [f"记住 {len(rules)} 条："]
            for rule in rules:
                tag = f"（{rule.tags[0]}）" if rule.tags else ""
                wrong = f" ✗{rule.wrong}" if rule.wrong else ""
                lines.append(f"{rule.id}. {rule.rule}{wrong}{tag}  权重{rule.weight:.1f}")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("删", "del", "删除"):
            if len(args) < 2 or not args[1].isdigit():
                yield event.plain_result("用法：/教 删 3")
                return
            ok = self.store.archive(int(args[1]))
            yield event.plain_result("删掉了。" if ok else "没找到这条。")
            return

        if action in ("清", "clear"):
            n = self.store.clear()
            yield event.plain_result(f"清空了 {n} 条。")
            return

        if action in ("关", "off"):
            self._runtime_enabled = False
            yield event.plain_result("已临时停用，不再学习和遵守说话规矩。")
            return

        if action in ("开", "on"):
            self._runtime_enabled = True
            yield event.plain_result("已恢复。")
            return

        if action in ("测试", "test"):
            text = " ".join(args[1:]).strip()
            if not text:
                yield event.plain_result("用法：/教 测试 你刚才说得不对，应该先问我感受")
                return
            level = judge_mod.detect(text)
            extracted = judge_mod.extract_by_rules(text)
            body = [f"档位：{level}"]
            if extracted:
                body.append("本地抽取：")
                for item in extracted:
                    body.append(f"  · {item['rule']}（置信 {item['confidence']:.2f}）")
            else:
                body.append("本地抽不出，会交给模型判定（若已开启）")
            yield event.plain_result("\n".join(body))
            return

        yield event.plain_result("不认识这个子命令，发 /教 看用法。")
