"""说话卡向量服务：语义判重 + 选卡打分。

为什么要它：卡片指纹（`card_fp`）只做去空白/标点的字面比对，模型把同一张卡
换个说法就会变成两张——面板上看起来像"重复登记"，注入时还会浪费字数。
这里用向量余弦相似度补上这一层：
- **判重**：新卡与现有卡的相似度 ≥ `emb_similar_threshold`（默认 0.88）视为同一张，
  只升权重/更新要点，不新建；
- **选卡**：注入前把当前消息也向量化，与场景描述算余弦，作为 `emb_pick_boost`
  权重的加成（默认 0.6），让"语气相近"的卡优先被选中。

配置（面板可改）：`emb_api_base` / `emb_api_key` / `emb_model` / `emb_dim`(默认 1024)。
`emb_api_base` 与 `emb_model` 任一留空 → 回落到 AstrBot 自带的 embedding provider。
任何失败都返回 None，调用方退回纯字面逻辑，绝不影响学习与注入。
"""

from __future__ import annotations

import hashlib
import math
import time
from typing import Any, Optional

from astrbot.api import logger

DEFAULT_DIM = 1024
_CACHE_MAX = 512
_TIMEOUT_SECONDS = 20.0


def _cfg(plugin: Any, key: str, default: Any) -> Any:
    getter = getattr(plugin, "_cfg", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    try:
        return plugin.config.get(key, default)
    except Exception:
        return default


def _norm(vec: Any) -> list[float]:
    try:
        return [float(x) for x in vec]
    except (TypeError, ValueError):
        return []


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / math.sqrt(na * nb)


class EmbeddingService:
    """把"文本→向量"收在一处，带内存缓存与降级。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self._cache: dict[str, list[float]] = {}
        self._last_error = ""
        self._observed_dim = 0

    @property
    def enabled(self) -> bool:
        return bool(_cfg(self.plugin, "emb_enabled", True))

    @property
    def dim(self) -> int:
        if self._observed_dim:
            return self._observed_dim
        try:
            return max(8, int(_cfg(self.plugin, "emb_dim", DEFAULT_DIM) or DEFAULT_DIM))
        except (TypeError, ValueError):
            return DEFAULT_DIM

    def _api_base(self) -> str:
        return str(_cfg(self.plugin, "emb_api_base", "") or "").strip()

    def _api_key(self) -> str:
        return str(_cfg(self.plugin, "emb_api_key", "") or "").strip()

    def _model(self) -> str:
        return str(_cfg(self.plugin, "emb_model", "") or "").strip()

    def model_label(self) -> str:
        """写进数据库的模型标识：换模型/维度后旧向量自动失效。"""
        if self._use_manual_api():
            return f"api:{self._model()}@{self.dim}"
        return f"astrbot:{self._astrbot_provider_id() or 'default'}@{self.dim}"

    def _astrbot_provider_id(self) -> str:
        return str(_cfg(self.plugin, "emb_provider_id", "") or "").strip()

    def threshold(self) -> float:
        try:
            value = float(_cfg(self.plugin, "emb_similar_threshold", 0.88) or 0.88)
        except (TypeError, ValueError):
            value = 0.88
        return min(0.999, max(0.5, value))

    def pick_boost(self) -> float:
        try:
            return max(0.0, min(2.0, float(_cfg(self.plugin, "emb_pick_boost", 0.6) or 0.6)))
        except (TypeError, ValueError):
            return 0.6

    @property
    def last_error(self) -> str:
        return self._last_error

    def mode(self) -> str:
        """astrbot = 用平台已配置的 embedding 模型；manual = 独立 API。

        判定顺序：①面板里选了平台模型（emb_provider_id 非空）→ astrbot；
        ②显式 emb_mode=manual 且填了 Base+模型 → manual；③否则按旧规则推断。
        """
        if self._astrbot_provider_id():
            return "astrbot"
        raw = str(_cfg(self.plugin, "emb_mode", "") or "").strip().lower()
        if raw == "astrbot":
            return "astrbot"
        return "manual" if (self._api_base() and self._model()) else "astrbot"

    def _use_manual_api(self) -> bool:
        return self.mode() == "manual" and bool(self._api_base()) and bool(self._model())

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "mode": self.mode(),
            "model": (self._model() if self._use_manual_api() else None)
            or self._astrbot_provider_id()
            or "AstrBot 默认",
            "dim": self.dim,
            "threshold": self.threshold(),
            "pick_boost": self.pick_boost(),
            "cached": len(self._cache),
            "last_error": self._last_error,
        }

    # ---------------------------------------------------------------- 主入口
    def cached(self, text: str) -> Optional[list[float]]:
        key = self._key(text)
        return self._cache.get(key)

    async def embed(self, text: str) -> Optional[list[float]]:
        vectors = await self.embed_many([text])
        return vectors[0] if vectors else None

    async def embed_many(self, texts: list[str]) -> list[Optional[list[float]]]:
        if not self.enabled or not texts:
            return [None for _ in texts]
        results: list[Optional[list[float]]] = [None for _ in texts]
        pending: list[tuple[int, str, str]] = []
        for idx, text in enumerate(texts):
            raw = str(text or "").strip()
            if not raw:
                continue
            key = self._key(raw)
            if key in self._cache:
                results[idx] = self._cache[key]
            else:
                pending.append((idx, key, raw))
        if not pending:
            return results

        vectors = await self._embed_remote([item[2] for item in pending])
        if vectors is None:
            return results
        for (idx, key, _raw), vec in zip(pending, vectors):
            if not vec:
                continue
            if not self._observed_dim:
                # 首次拿到向量后记住真实维度：配置里填错也不会拿不同维度的向量互相比
                self._observed_dim = len(vec)
            results[idx] = vec
            self._cache[key] = vec
            if len(self._cache) > _CACHE_MAX:
                # 简单淘汰：清掉最早的一批
                for stale in list(self._cache.keys())[: max(1, _CACHE_MAX // 4)]:
                    self._cache.pop(stale, None)
        return results

    def _key(self, text: str) -> str:
        payload = f"{self.model_label()}\x00{text}".encode("utf-8", "ignore")
        return hashlib.sha1(payload).hexdigest()

    # ---------------------------------------------------------------- 远程调用
    async def _embed_remote(self, texts: list[str]) -> Optional[list[Optional[list[float]]]]:
        if self._use_manual_api():
            result = await self._embed_via_http(self._api_base(), self._model(), texts)
            if result is not None:
                return result
            logger.debug("[speech_lessons] 独立 embedding API 失败，尝试回落 AstrBot")
        return await self._embed_via_astrbot(texts)

    async def _embed_via_http(
        self, base: str, model: str, texts: list[str]
    ) -> Optional[list[Optional[list[float]]]]:
        import asyncio

        try:
            import aiohttp
        except Exception:  # noqa: BLE001 - 环境没有 aiohttp 就只走 AstrBot 接口
            self._last_error = "缺少 aiohttp，无法调用独立 embedding API"
            return None

        url = base.rstrip("/")
        if not url.endswith("/embeddings"):
            url = f"{url}/embeddings"
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body = {"model": model, "input": texts}
        started = time.monotonic()
        try:
            timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=body, headers=headers) as resp:
                    if resp.status >= 400:
                        detail = (await resp.text())[:200]
                        self._last_error = f"embedding HTTP {resp.status}: {detail}"
                        logger.warning(f"[speech_lessons] {self._last_error}")
                        return None
                    data = await resp.json(content_type=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"embedding 请求失败: {exc}"
            logger.warning(f"[speech_lessons] {self._last_error}")
            return None

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            self._last_error = "embedding 返回结构异常"
            return None
        ordered: list[Optional[list[float]]] = [None for _ in texts]
        for pos, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            target = pos if not isinstance(idx, int) else idx
            if not (0 <= target < len(texts)):
                continue
            vec = _norm(item.get("embedding"))
            if vec:
                ordered[target] = vec
        self._last_error = ""
        logger.debug(
            f"[speech_lessons] embedding(api) {len(texts)} 条，耗时 {time.monotonic() - started:.2f}s"
        )
        return ordered

    async def _embed_via_astrbot(
        self, texts: list[str]
    ) -> Optional[list[Optional[list[float]]]]:
        provider = await self._find_provider()
        if provider is None:
            self._last_error = "没有可用的 AstrBot embedding provider"
            return None
        try:
            batch = getattr(provider, "get_embeddings", None)
            if callable(batch):
                try:
                    vectors = await batch(list(texts))
                    if isinstance(vectors, list) and len(vectors) == len(texts):
                        out = [_norm(v) for v in vectors]
                        if any(out):
                            self._last_error = ""
                            return out
                except Exception:  # noqa: BLE001 - 批量不支持就逐条
                    pass
            single = getattr(provider, "get_embedding", None)
            if not callable(single):
                self._last_error = "embedding provider 没有 get_embedding 方法"
                return None
            out: list[Optional[list[float]]] = []
            for text in texts:
                out.append(_norm(await single(text)))
            self._last_error = ""
            return out
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"AstrBot embedding 调用失败: {exc}"
            logger.warning(f"[speech_lessons] {self._last_error}")
            return None

    async def _find_provider(self) -> Any:
        try:
            manager = getattr(self.plugin.context, "provider_manager", None)
        except Exception:
            manager = None
        if manager is None:
            return None
        wanted = self._astrbot_provider_id()
        getter = getattr(manager, "get_provider_by_id", None)
        if wanted and callable(getter):
            try:
                result = getter(wanted)
                if hasattr(result, "__await__"):
                    result = await result
                if result is not None:
                    return result
            except Exception:
                pass
        insts = getattr(manager, "embedding_provider_insts", None)
        if isinstance(insts, list) and insts:
            return insts[0]
        return None
