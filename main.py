"""教我说话 v2 —— 主人在私聊里教一次，它就学会那个口气。

设计原则：
1. 只有主人能在私聊里教（群里教一律不学）；
2. 学到的是「说话卡」（场景 / 反例 / 正例 / 一句原则），不是正则抽出来的碎句子；
3. 两张入口：她自己调 learn_speech_style 工具，或私聊钩子兜底（双保险，同一句话只学一次）；
4. 注入时渲染成"别这么说 → 该这么说"的对照示例，比祈使句更能改语气；
5. 图片范例走多模态模型提炼（聊天截图也认）；
6. 复习推进只认主人的明确反馈，不做「注入即复习成功」的伪复习。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as MsgImage
from astrbot.api.star import Context, Star, StarTools

try:  # 面板可视化视图用的 Web API 助手（AstrBot 新版本提供）
    from astrbot.api.web import error_response, json_response, request

    _WEB_OK = True
except Exception:  # noqa: BLE001 - 老版本没有这套 API 时，插件其余功能照常
    error_response = None  # type: ignore[assignment]
    json_response = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    _WEB_OK = False

from . import judge as judge_mod
from . import learner as learner_mod
from .store import CardStore, norm_key

PLUGIN_NAME = "astrbot_plugin_speech_lessons"
# 说话卡以「临时用户内容片段」注入本轮请求：只发给模型、不写会话历史，
# 且不触碰 system_prompt（它必须跨轮稳定，否则服务端前缀缓存全部失效）。
INJECT_HEADER = "【说话卡参考 · 主人在私聊里教的口气示范】"
MAX_TASK_KEEP = 16
REPLY_CACHE_TTL = 1800.0
REPLY_CACHE_MAX = 64
CLAIM_TTL = 240.0


def _get_text_part():
    """延迟取 TextPart。

    TextPart 在 astrbot.core.* 内部模块，不是公共 API；缺这个机制时（老版本）
    本轮就不注入，绝不回退去改 system_prompt。
    """
    try:
        from astrbot.core.agent.message import TextPart
    except Exception:  # pragma: no cover - 取决于 AstrBot 版本
        return None
    return TextPart


class SpeechLessonsPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config  # type: ignore[assignment]

        data_dir = self._resolve_data_dir()
        self.store = CardStore(os.path.join(data_dir, "lessons.db"))

        self._runtime_enabled = True
        self._inject_ids_by_session: dict[str, list[int]] = {}
        self._pending_notes: dict[str, str] = {}
        self._last_reply: dict[str, tuple[str, float]] = {}
        self._claimed: dict[str, float] = {}
        self._buffers: dict[str, dict[str, Any]] = {}
        self._learn_tasks: set[asyncio.Task] = set()
        self._maint_task: asyncio.Task | None = None
        self._last_decay_at = 0.0

        self._register_web_apis()

        logger.info(
            "[speech_lessons] v2 已加载，说话卡=%d，数据目录=%s",
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
        tasks = [self._maint_task, *self._learn_tasks]
        for buf in list(self._buffers.values()):
            task = buf.get("task")
            if task:
                tasks.append(task)
        self._buffers.clear()
        for task in tasks:
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
                logger.info("[speech_lessons] 闲置衰减归档 %d 张卡", archived)
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
        for sep in ("，", "；", ";", " ", "\n", "|"):
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

    # ================================================================== 上下文
    async def _provider_id(self, umo: str) -> str:
        pid = str(self._cfg("learn_provider_id", "") or "").strip()
        if pid:
            return pid
        try:
            return await self.context.get_current_chat_provider_id(umo=umo) or ""
        except Exception:
            return ""

    def _direct_api(self) -> tuple[str, str, str]:
        return (
            str(self._cfg("learn_api_base", "") or "").strip(),
            str(self._cfg("learn_api_key", "") or "").strip(),
            str(self._cfg("learn_model", "") or "").strip(),
        )

    def _has_image(self, event: AstrMessageEvent) -> bool:
        try:
            chain = event.message_obj.message or []
        except Exception:
            return False
        return any(isinstance(comp, MsgImage) for comp in chain)

    async def _image_paths(self, event: AstrMessageEvent) -> list[str]:
        if not bool(self._cfg("vision_enabled", True)):
            return []
        try:
            chain = event.message_obj.message or []
        except Exception:
            return []
        paths: list[str] = []
        for comp in chain:
            if not isinstance(comp, MsgImage):
                continue
            try:
                path = await comp.convert_to_file_path()
                if path:
                    paths.append(str(path))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[speech_lessons] 取图失败: %s", exc)
        return paths[:2]

    def _last_reply_text(self, umo: str) -> str:
        item = self._last_reply.get(umo)
        if not item:
            return ""
        text, ts = item
        if time.time() - ts > REPLY_CACHE_TTL:
            return ""
        return text

    # --------------------------------------------------- 同一条教学只学一次
    def _claim(self, umo: str, note: str) -> str:
        """返回本次学习的 claim key；已被占用则返回空串。"""
        now = time.time()
        for key, ts in list(self._claimed.items()):
            if now - ts > CLAIM_TTL:
                self._claimed.pop(key, None)
        key = f"{umo}|{norm_key(note)[:60]}"
        if key in self._claimed:
            return ""
        self._claimed[key] = now
        return key

    def _release(self, key: str) -> None:
        if key:
            self._claimed.pop(key, None)

    # ================================================================== 学习
    async def _learn(
        self,
        *,
        umo: str,
        note: str,
        images: list[str] | None = None,
        last_reply: str = "",
        source: str = "text",
    ) -> dict[str, Any] | None:
        """调模型提炼说话卡并入库。失败返回 None。"""
        api_base, api_key, model = self._direct_api()
        provider_id = "" if (api_base and model) else await self._provider_id(umo)
        if not provider_id and not (api_base and model):
            logger.debug("[speech_lessons] 拿不到 provider，跳过提炼")
            return None
        timeout = float(self._cfg("learn_timeout_seconds", 15.0))
        result = await learner_mod.learn_cards(
            context=self.context,
            note=note,
            last_reply=last_reply,
            images=images or [],
            quotes=judge_mod.quoted(note),
            provider_id=provider_id,
            api_base=api_base,
            api_key=api_key,
            model=model,
            timeout=timeout,
            card_max_chars=int(self._cfg("card_max_chars", 120)),
            min_confidence=float(self._cfg("min_confidence", 0.55)),
        )
        if not result:
            return None
        min_conf = float(self._cfg("min_confidence", 0.55))
        cards = [
            c for c in result.get("cards") or [] if float(c.get("confidence") or 0) >= min_conf
        ]
        if not cards:
            logger.info(
                "[speech_lessons] 提炼置信度不足(%.2f)，不学",
                float(result.get("confidence") or 0.0),
            )
            return None
        stored = self._store_cards(cards, note, source)
        if not stored:
            return None
        return {"cards": stored, "count": len(stored)}

    def _store_cards(self, cards: list[dict[str, Any]], raw_text: str, source: str) -> list[dict[str, Any]]:
        max_cards = int(self._cfg("max_cards", 200))
        weight = float(self._cfg("new_card_weight", 1.5))
        out: list[dict[str, Any]] = []
        for card in cards:
            if self.store.count_active() >= max_cards:
                logger.warning("[speech_lessons] 说话卡已达上限 %d，忽略本次教学", max_cards)
                break
            card_id, created = self.store.add(
                scene=card.get("scene", ""),
                bad=card.get("bad", ""),
                good=card.get("good", ""),
                principle=card.get("principle", ""),
                tags=card.get("tags") or [],
                confidence=float(card.get("confidence") or 0.6),
                source=source,
                weight=weight,
                raw_text=raw_text[:500],
            )
            if card_id:
                out.append({**card, "id": card_id, "created": created})
        if out:
            logger.info("[speech_lessons] 学到 %d 张说话卡", len(out))
        return out

    # ================================================================== 钩子
    @filter.on_llm_response()
    async def remember_reply(self, event: AstrMessageEvent, resp: Any):
        """兜底记录本轮回复（可能带标记/标签，后面会被 on_decorating_result 覆盖）。"""
        umo = self._session_key(event)
        if not umo:
            return
        text = self._clean_reply(str(getattr(resp, "completion_text", "") or ""))
        if text:
            self._remember_text(umo, text)

    @filter.on_decorating_result(priority=-200000)
    async def remember_final_reply(self, event: AstrMessageEvent):
        """抓用户真正看到的那句话：结果链里的纯文本，最贴合"反例"用途。

        排在 private_companion 的 on_decorating_result(20000~-18000) 之后，
        所以拿到的是它改完（去分段标记/审校）之后的文本。
        """
        try:
            result = event.get_result()
        except Exception:
            return
        chain = getattr(result, "chain", None) or []
        parts: list[str] = []
        for comp in chain:
            name = type(comp).__name__
            if name == "Plain":
                text = str(getattr(comp, "text", "") or "").strip()
                if text:
                    parts.append(text)
        if not parts:
            return
        umo = self._session_key(event)
        if not umo:
            return
        merged = self._clean_reply(" ".join(parts))
        if merged:
            self._remember_text(umo, merged)

    @staticmethod
    def _clean_reply(text: str) -> str:
        """去掉分段标记、TTS 标签、HTML 注释等非人话噪声。"""
        if not text:
            return ""
        out = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        out = re.sub(r"\[(?:TTS|tts|语音|表情|图片)[^\]]{0,60}\]", "", out)
        out = re.sub(r"[ \t]+", " ", out)
        return out.strip()[:400]

    def _remember_text(self, umo: str, text: str) -> None:
        if not text:
            return
        if len(self._last_reply) > REPLY_CACHE_MAX:
            oldest = min(self._last_reply.items(), key=lambda kv: kv[1][1])[0]
            self._last_reply.pop(oldest, None)
        self._last_reply[umo] = (text[:600], time.time())

    # --------------------------------------------------- 捕获（带分片防抖）
    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE, priority=250000
    )
    async def capture_lesson(self, event: AstrMessageEvent):
        """私聊里识别「在教」的消息。只有主人说话才算。

        优先级 250000：必须排在 private_companion(220000/0) 之前——
        它回复完会 stop_event()，而消息分发循环遇到 stopped 就 break，
        排在它后面等于这条钩子永远不执行。
        """
        if not self._enabled() or not bool(self._cfg("capture_enabled", True)):
            return
        if not self._is_owner(event):
            return
        note = str(getattr(event, "message_str", "") or "").strip()
        if judge_mod.is_command(note) or len(note) > 500:
            return
        has_image = self._has_image(event)
        if not note and not has_image:
            return

        umo = self._session_key(event)
        feedback = judge_mod.detect_feedback(note)

        # 1) 夸：推进 SM-2，本轮不用再学
        if feedback is True and judge_mod.detect(note, has_image=has_image) == "chat":
            ids = self._inject_ids_by_session.get(umo) or []
            if ids:
                changed = self.store.feedback(ids, positive=True)
                if changed:
                    logger.info("[speech_lessons] 正面反馈命中 %d 张卡", changed)
            return

        # 2) 嫌：先把上一轮注入的卡降权，再学新的
        if feedback is False:
            ids = self._inject_ids_by_session.get(umo) or []
            if ids:
                changed = self.store.feedback(ids, positive=False)
                if changed:
                    logger.info("[speech_lessons] 负反馈降权 %d 张卡", changed)
            self._pending_notes[umo] = (
                "主人刚说你刚才那条又不对了，这轮先按他的意思改，不要辩解。"
            )

        # 3) 是不是在教
        level = judge_mod.detect(note, has_image=has_image)
        if feedback is None and level == "chat":
            return

        # 4) 进缓冲区：图 + 文字常常分两条发，reply_merge 也在这个 stage 之后才合并，
        #    所以自己攒一个小窗口再一次性提炼，避免学成"半条"。
        images = await self._image_paths(event)
        await self._buffer_capture(umo, note, images, bool(has_image))

    async def _buffer_capture(
        self, umo: str, note: str, images: list[str], has_image: bool
    ) -> None:
        buf = self._buffers.get(umo)
        if buf is None:
            buf = {"parts": [], "images": [], "has_image": False, "task": None}
            self._buffers[umo] = buf
        if note:
            buf["parts"].append(note)
        for path in images:
            if path and path not in buf["images"]:
                buf["images"].append(path)
        buf["has_image"] = buf["has_image"] or has_image
        buf["ts"] = time.time()

        old = buf.get("task")
        if old and not old.done():
            old.cancel()
        task = asyncio.create_task(self._flush_buffer(umo))
        buf["task"] = task

    async def _flush_buffer(self, umo: str) -> None:
        await asyncio.sleep(float(self._cfg("capture_debounce_seconds", 9.0)))
        buf = self._buffers.pop(umo, None)
        if not buf:
            return
        parts = [p for p in buf["parts"] if p]
        images = list(buf["images"])
        if not parts and not images:
            return
        note = "\n".join(parts)[:800]

        key = self._claim(umo, note or f"img:{len(images)}")
        if not key:
            return
        learned = False
        try:
            result = await self._learn(
                umo=umo,
                note=note,
                images=images,
                last_reply=self._last_reply_text(umo),
                source="image" if images else "chat",
            )
            if result:
                learned = True
                self._pending_notes[umo] = (
                    "主人刚教过你一次说话方式，这轮按他给的口气来，别辩解、别复述规矩本身。"
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[speech_lessons] 后台学习失败: %s", exc)
        finally:
            # 学成功了就一直占着 claim（4 分钟内同一句不重复学），失败才放开重试
            if not learned:
                self._release(key)

    def _drop_buffer(self, umo: str) -> None:
        """工具/指令已经学过时，丢掉同会话里还没到期的缓冲，避免重复调模型。"""
        buf = self._buffers.pop(umo, None)
        if not buf:
            return
        task = buf.get("task")
        if task and not task.done():
            task.cancel()

    # ================================================================== 注入
    def _pick_cards(self, current_text: str):
        now = time.time()
        cards = self.store.active(limit=int(self._cfg("scan_cards", 200)))
        if not cards:
            return []
        text = current_text or ""

        def score(card) -> float:
            value = card.weight
            if card.next_review_at and card.next_review_at <= now:
                value += 0.3
            if card.scene:
                tokens = [
                    t for t in re.split(r"[，。！？、；：\s（）()「」\[\]]", card.scene) if len(t) >= 2
                ]
                if any(t in text for t in tokens):
                    value += 0.6
            if card.good and len(card.good) >= 4 and card.good in text:
                value += 0.2
            return value

        cards.sort(key=score, reverse=True)
        limit = int(self._cfg("inject_max_cards", 3))
        budget = int(self._cfg("inject_max_chars", 900))
        picked: list[Any] = []
        used = 0
        for card in cards:
            length = (
                len(card.scene)
                + len(card.bad)
                + len(card.good)
                + len(card.principle)
                + 24
            )
            if picked and used + length > budget:
                break
            picked.append(card)
            used += length
            if len(picked) >= limit:
                break
        return picked

    @filter.on_llm_request(priority=-100)
    async def inject_cards(self, event: AstrMessageEvent, req: Any):
        """把说话卡作为「临时用户内容片段」注入本轮请求。

        为什么不写 system_prompt：system_prompt 是模型收到的第一条消息，
        每轮往里塞会变的说话卡会让服务端前缀缓存全部失效，也破坏
        「系统提示词 = 稳定人格」的分层。改用 req.extra_user_content_parts +
        mark_as_temp()：片段并在本轮 user 内容之后，且打上 _no_save 不落盘。
        """
        if not self._enabled() or not bool(self._cfg("inject_enabled", True)):
            return
        text = str(getattr(event, "message_str", "") or "")
        if judge_mod.is_command(text):
            return
        try:
            picked = self._pick_cards(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[speech_lessons] 读取说话卡失败: %s", exc)
            return
        umo = self._session_key(event)
        note = self._pending_notes.pop(umo, "")
        if not picked and not note:
            return

        block = self._render_inject_block(picked, note)
        if not block:
            return
        if not self._append_temp_part(req, block):
            logger.debug(
                "[speech_lessons] 当前 AstrBot 不支持 extra_user_content_parts，本轮不注入"
            )
            return

        if picked:
            self._inject_ids_by_session[umo] = [card.id for card in picked]
            try:
                self.store.touch([card.id for card in picked])
            except Exception:
                pass

    def _render_inject_block(self, picked: list[Any], note: str) -> str:
        parts: list[str] = []
        if picked:
            lines = [
                learner_mod.render_card(card, i) for i, card in enumerate(picked, 1)
            ]
            parts.append(
                f"{INJECT_HEADER}\n"
                + "\n".join(lines)
                + "\n（只是口气参考：别逐字复述例子，也别提到这些卡本身。）"
            )
        if note and bool(self._cfg("confirm_note", True)):
            parts.append(f"【本轮附注】{note}")
        return "\n".join(parts).strip()

    @staticmethod
    def _append_temp_part(req: Any, block: str) -> bool:
        """把文本块追加成本轮临时内容片段；返回是否成功。"""
        text_part_cls = _get_text_part()
        if text_part_cls is None:
            return False
        parts = getattr(req, "extra_user_content_parts", None)
        if not isinstance(parts, list):
            return False
        try:
            part = text_part_cls(text=block)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[speech_lessons] 构造临时片段失败: %s", exc)
            return False
        mark = getattr(part, "mark_as_temp", None)
        if callable(mark):
            try:
                mark()
            except Exception as exc:  # noqa: BLE001 - 标记失败仍可注入，只是会进历史
                logger.debug("[speech_lessons] 临时标记失败: %s", exc)
        parts.append(part)
        return True

    # ============================================================ 面板 Web API
    def _register_web_apis(self) -> None:
        if not _WEB_OK:
            logger.debug("[speech_lessons] 当前 AstrBot 没有 astrbot.api.web，跳过面板 API")
            return
        apis = [
            (f"/{PLUGIN_NAME}/cards", self.page_list_cards, ["GET"], "教说话：列出说话卡"),
            (f"/{PLUGIN_NAME}/cards/save", self.page_save_card, ["POST"], "教说话：新建/修改说话卡"),
            (f"/{PLUGIN_NAME}/cards/<card_id>/archive", self.page_archive_card, ["POST"], "教说话：归档说话卡"),
            (f"/{PLUGIN_NAME}/cards/<card_id>/restore", self.page_restore_card, ["POST"], "教说话：恢复说话卡"),
            (f"/{PLUGIN_NAME}/cards/<card_id>/delete", self.page_delete_card, ["POST"], "教说话：彻底删除说话卡"),
        ]
        for route, handler, methods, desc in apis:
            try:
                self.context.register_web_api(route, handler, methods, desc)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[speech_lessons] 面板 API 注册失败 %s: %s", route, exc)
        logger.info("[speech_lessons] 面板 API 已注册 %d 个", len(apis))

    async def page_list_cards(self):
        status = str(request.query.get("status", "active") or "active")
        keyword = str(request.query.get("q", "") or "").strip()
        try:
            limit = int(request.query.get("limit", 300) or 300)
        except (TypeError, ValueError):
            limit = 300
        try:
            items = self.store.query(
                status=status, keyword=keyword, limit=max(1, min(limit, 500))
            )
            return json_response(
                {"ok": True, "items": items, "stats": self.store.stats()}
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[speech_lessons] 面板读取失败: %s", exc)
            return error_response(f"读取失败：{exc}", status_code=500)

    async def page_save_card(self):
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON 对象")
        good = str(payload.get("good") or "").strip()
        bad = str(payload.get("bad") or "").strip()
        principle = str(payload.get("principle") or "").strip()
        if not good and not bad:
            return error_response("正例和反例至少要填一个")
        max_chars = int(self._cfg("card_max_chars", 120))
        for label, value in (("场景", payload.get("scene")), ("反例", bad), ("正例", good), ("要点", principle)):
            if len(str(value or "")) > max_chars * 2:
                return error_response(f"{label}太长了（上限 {max_chars * 2} 字）")
        status = str(payload.get("status") or "active")
        if status not in ("active", "archived"):
            status = "active"

        raw_id = payload.get("id")
        if raw_id not in (None, "", 0, "0"):
            try:
                card_id = int(raw_id)
            except (TypeError, ValueError):
                return error_response("id 不合法")
            ok = self.store.update_fields(
                card_id,
                {
                    "scene": payload.get("scene", ""),
                    "bad": bad,
                    "good": good,
                    "principle": principle,
                    "tags": payload.get("tags", []),
                    "weight": payload.get("weight", 1.5),
                    "status": status,
                },
            )
            if not ok:
                return error_response("没找到这张卡（可能已被删除）", status_code=404)
            return json_response({"ok": True, "id": card_id, "created": False})

        card_id, created = self.store.add(
            scene=str(payload.get("scene") or "").strip(),
            bad=bad,
            good=good,
            principle=principle,
            tags=payload.get("tags") or [],
            confidence=0.9,
            source="panel",
            weight=float(payload.get("weight") or 1.5),
            raw_text="面板手动创建",
        )
        if not card_id:
            return error_response("保存失败：正例/反例不能都为空")
        if status != "active":
            self.store.update_fields(card_id, {"status": status})
        return json_response({"ok": True, "id": card_id, "created": created})

    async def page_archive_card(self, card_id: str):
        return self._card_action(card_id, "archive")

    async def page_restore_card(self, card_id: str):
        return self._card_action(card_id, "restore")

    async def page_delete_card(self, card_id: str):
        return self._card_action(card_id, "delete")

    def _card_action(self, card_id: str, action: str):
        try:
            target = int(str(card_id).strip())
        except (TypeError, ValueError):
            return error_response("卡片 id 不合法")
        if action == "archive":
            ok = self.store.archive(target)
        elif action == "restore":
            ok = self.store.restore(target)
        else:
            ok = self.store.delete(target)
        if not ok:
            return error_response("没找到这张卡", status_code=404)
        label = {"archive": "已归档", "restore": "已恢复", "delete": "已删除"}[action]
        return json_response({"ok": True, "message": label})

    # ================================================================== 工具
    @filter.llm_tool(name="learn_speech_style")
    async def learn_speech_style_tool(
        self, event: AstrMessageEvent, note: str = "", focus: str = ""
    ) -> str | None:
        """把主人这次"教你说话"的内容学下来（存成说话卡：场景/反例/正例/要点）。

        什么时候调用：主人正在纠正你的表达方式，或给你优秀回复范例要你学。
        例如"这样不对，AI 味太重""太机械了，别说教""学学你同类，这样回"
        "以后别这样接话"，或者主人发来聊天截图/范例并让你照做。
        什么时候不要调用：普通聊天、提问、夸你、主人只是吐槽别人或转述别人说话。
        只用于"表达方式/口气"；主人要记的是事实、事件、约定时，用
        memory_companion_remember 之类的记忆工具，不要调本工具。
        同一句话只需要调一次；学过之后不要向主人复述规则内容。

        Args:
            note(string): 主人这次教你的原话，尽量原样照抄。
            focus(string): 你想额外强调的重点（如"别反问"），没有就留空。
        """
        # 未授权/非私聊/关闭时返回 None：不往 prompt 里塞拒绝文案，避免污染对话
        if not self._enabled() or not bool(self._cfg("learn_enabled", True)):
            return None
        if not self._is_owner(event):
            return None
        try:
            is_private = bool(event.is_private_chat())
        except Exception:
            is_private = True
        if not is_private:
            return None

        raw = str(getattr(event, "message_str", "") or "").strip()
        text = (note or raw).strip()
        if focus:
            text = f"{text}\n（重点：{focus}）"
        if not text:
            return "没听清这次的教学内容，没有记录。"

        umo = self._session_key(event)
        key = self._claim(umo, raw or text)
        if not key:
            return "这次的说法刚刚已经记下了，不用重复学，顺着主人的口气回一句就行。"
        images = await self._image_paths(event)
        result = None
        try:
            result = await self._learn(
                umo=umo,
                note=text,
                images=images,
                last_reply=self._last_reply_text(umo),
                source="image" if images else "tool",
            )
        finally:
            if not result:
                self._release(key)
        if not result:
            return "这次没能提炼出可用的说法（可能内容太模糊或模型超时），没有写进规矩。"
        self._drop_buffer(umo)
        scenes = [c.get("scene") for c in result["cards"] if c.get("scene")]
        tail = f"（场景：{scenes[0]}）" if scenes else ""
        return (
            f"已学会 {result['count']} 张说话卡{tail}。"
            "接下来用主人的口气回一句就好，不要复述规则、不要提这件事。"
        )

    @filter.llm_tool(name="list_speech_styles")
    async def list_speech_styles_tool(self, event: AstrMessageEvent) -> str | None:
        """查看主人教过你的说话卡清单（id + 摘要）。

        什么时候调用：主人问"我教过你什么说法/你都学了什么"时。不需要任何参数。
        """
        if not self._is_owner(event):
            return None
        cards = self.store.active(limit=30)
        if not cards:
            return "还没有任何说话卡。"
        lines = [f"共 {len(cards)} 张："]
        for card in cards:
            lines.append(f"{card.id}. {learner_mod.card_line(card)}")
        return "\n".join(lines)

    @filter.llm_tool(name="forget_speech_style")
    async def forget_speech_style_tool(
        self, event: AstrMessageEvent, card_id: str = ""
    ) -> str | None:
        """删掉一张主人教过的说话卡。

        什么时候调用：主人明确说"忘掉第 3 条/那条说错了"时。不确定 id 先调 list_speech_styles。

        Args:
            card_id(string): 要删的说话卡 id 数字。
        """
        if not self._is_owner(event):
            return None
        try:
            target = int(str(card_id).strip())
        except (TypeError, ValueError):
            return "需要一个数字 id，先用 list_speech_styles 看看有哪些。"
        ok = self.store.archive(target)
        return "删掉了。" if ok else "没找到这张卡。"

    # ================================================================== 指令
    @filter.command("教")
    async def cmd_teach(self, event: AstrMessageEvent):
        """/教 列表 · /教 学 <原话> · /教 删 3 · /教 清 · /教 关 · /教 开 · /教 测试 <话>"""
        if not self._is_owner(event):
            yield event.plain_result("这个只有主人能用。")
            return

        raw = str(getattr(event, "message_str", "") or "").strip()
        args = raw.split()[1:]
        if not args:
            yield event.plain_result(
                "用法：\n"
                "/教 列表 —— 看我学会了哪些说话卡\n"
                "/教 学 <原话> —— 直接教一次（不必靠模型自己判断）\n"
                "/教 删 3 —— 删掉第 3 张\n"
                "/教 清 —— 清空全部\n"
                "/教 关 / 开 —— 临时停用/恢复\n"
                "/教 测试 <一句话> —— 看这句话会不会被当成教学"
            )
            return

        action = args[0]

        if action in ("列表", "list"):
            cards = self.store.active(limit=50)
            if not cards:
                yield event.plain_result("还没有记住任何说话卡。")
                return
            lines = [f"记住 {len(cards)} 张："]
            for card in cards:
                lines.append(
                    f"{card.id}. {learner_mod.card_line(card)}  权重{card.weight:.1f}"
                )
            yield event.plain_result("\n".join(lines))
            return

        if action in ("学", "learn"):
            note = " ".join(args[1:]).strip()
            if not note:
                yield event.plain_result("用法：/教 学 别人说难绷的时候你应该说绷不住了")
                return
            umo = self._session_key(event)
            key = self._claim(umo, raw)
            if not key:
                yield event.plain_result("这句刚学过，跳过。")
                return
            images = await self._image_paths(event)
            result = None
            try:
                result = await self._learn(
                    umo=umo,
                    note=note,
                    images=images,
                    last_reply=self._last_reply_text(umo),
                    source="command",
                )
            finally:
                if not result:
                    self._release(key)
            if not result:
                yield event.plain_result("没提炼出可用的说法（太模糊或模型超时）。")
                return
            self._drop_buffer(umo)
            lines = [f"学会 {result['count']} 张："]
            for card in result["cards"]:
                mark = "新" if card.get("created") else "加强"
                lines.append(f"[{mark}] {learner_mod.card_line(card)}")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("删", "del", "删除"):
            if len(args) < 2 or not args[1].isdigit():
                yield event.plain_result("用法：/教 删 3")
                return
            ok = self.store.archive(int(args[1]))
            yield event.plain_result("删掉了。" if ok else "没找到这张卡。")
            return

        if action in ("清", "clear"):
            n = self.store.clear()
            yield event.plain_result(f"清空了 {n} 张。")
            return

        if action in ("关", "off"):
            self._runtime_enabled = False
            yield event.plain_result("已临时停用，不再学习和使用说话卡。")
            return

        if action in ("开", "on"):
            self._runtime_enabled = True
            yield event.plain_result("已恢复。")
            return

        if action in ("测试", "test"):
            text = " ".join(args[1:]).strip()
            if not text:
                yield event.plain_result("用法：/教 测试 这样不对，AI味太重了")
                return
            level = judge_mod.detect(text, has_image=False)
            feedback = judge_mod.detect_feedback(text)
            body = [f"档位：{level}"]
            if feedback is False:
                body.append("反馈：负面 → 会把上一轮的卡降权，然后学新的")
            elif feedback is True:
                body.append("反馈：正面 → 推进复习间隔，不学新内容")
            if level == "chat" and feedback is None:
                body.append("结论：不会触发学习（也不烧模型 token）")
            else:
                body.append("结论：会触发提炼（1 次模型调用，含看图）")
            quoted = judge_mod.quoted(text)
            if quoted:
                body.append("引号原文：" + " / ".join(quoted[:3]))
            yield event.plain_result("\n".join(body))
            return

        yield event.plain_result("不认识这个子命令，发 /教 看用法。")
