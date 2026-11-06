"""把主人的一次"教"提炼成说话卡（场景 / 反例 / 正例 / 原则）。

两条路都支持，留空就走 AstrBot 自带 provider：
1) AstrBot Provider：context.llm_generate(chat_provider_id=..., image_urls=[...])
2) 直连 OpenAI 兼容接口：api_base + api_key + model（不需要在 AstrBot 里配 provider）

失败一律返回 None —— 宁可不学，也不要乱学。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from typing import Any, Optional

LEARN_PROMPT = """你在帮一个 AI 学习"怎么说话"。主人刚给了它一次纠正/示范。

主人的原话：
<<NOTE>>

<<LAST_REPLY>><<IMAGE>><<QUOTED>>
请判断：主人这次是不是在教它说话/教它换个说法？
只是普通聊天、提问、撒娇、陈述事实 → is_teaching = false。

是 → 提炼 1~3 张「说话卡」。严格要求：
1. bad / good 必须尽量用**原文**（主人打的字、我上一轮的回复、图片里的原话），
   不要自己改写、不要润色，不要编造主人没说过的示范。
2. 主人只抱怨了、没给正确说法 → good 留空，把「该怎么接」写进 principle。
3. 主人只给了优秀范例（说"学学"之类）→ good 填范例原文，bad 可以留空。
4. scene 写"什么场面用"：对方说什么话、什么情绪、什么场合；通用的留空。
5. principle 是一句照做就行的话，不超过 50 字，写「说话方式」而不是「态度口号」。
6. 图片可能是聊天截图：把截图里主人的纠正和合格回复的关键原话抄进 bad / good。
7. 宁少勿滥。只有一条真实可用的做法，就只给一张卡。

只输出 JSON，不要解释、不要 markdown：
{"is_teaching": true, "confidence": 0.0-1.0,
 "cards": [{"scene": "", "bad": "", "good": "", "principle": "", "tags": ["语气"], "confidence": 0.0-1.0}]}
"""

_TAGS_HINT = ("语气", "称呼", "接梗", "长度", "情绪", "边界", "话题")


def _parse_json(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    block = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = block.group(1) if block else text
    m = re.search(r"\{[\s\S]*\}", candidate)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _build_prompt(note: str, last_reply: str, has_image: bool, quotes: list[str]) -> str:
    last_reply_block = ""
    if last_reply:
        last_reply_block = (
            "我上一轮的回复（可能是被嫌的那句反例，也可能无关）：\n"
            f"{last_reply}\n\n"
        )
    image_block = ""
    if has_image:
        image_block = "这次主人还发了图片（可能是聊天截图或范例），请先看图再判断。\n\n"
    quoted_block = ""
    if quotes:
        quoted_block = "主人括号/引号里点出来的原文：\n" + "\n".join(
            f"- {q}" for q in quotes[:3]
        ) + "\n\n"
    return (
        LEARN_PROMPT.replace("<<NOTE>>", (note or "")[:800])
        .replace("<<LAST_REPLY>>", last_reply_block)
        .replace("<<IMAGE>>", image_block)
        .replace("<<QUOTED>>", quoted_block)
    )


def _clean_cards(data: dict[str, Any], card_max_chars: int) -> list[dict[str, Any]]:
    cards = data.get("cards")
    if not isinstance(cards, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in cards[:3]:
        if not isinstance(raw, dict):
            continue
        scene = str(raw.get("scene") or "").strip()[:card_max_chars]
        bad = str(raw.get("bad") or "").strip()[:card_max_chars]
        good = str(raw.get("good") or "").strip()[:card_max_chars]
        principle = str(raw.get("principle") or "").strip()[:card_max_chars]
        if not principle and not good and not bad:
            continue
        if not good and not bad:
            # 只有一句原则：可以，但要求它足够具体
            if len(principle) < 4:
                continue
        tags = raw.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip()[:12] for t in tags if str(t).strip()][:4]
        try:
            conf = float(raw.get("confidence") or data.get("confidence") or 0.6)
        except (TypeError, ValueError):
            conf = 0.6
        out.append(
            {
                "scene": scene,
                "bad": bad,
                "good": good,
                "principle": principle,
                "tags": tags or ["语气"],
                "confidence": max(0.0, min(1.0, conf)),
            }
        )
    return out


async def _call_via_provider(
    context: Any,
    provider_id: str,
    prompt: str,
    images: list[str],
    timeout: float,
) -> str:
    kwargs: dict[str, Any] = {"chat_provider_id": provider_id, "prompt": prompt}
    if images:
        kwargs["image_urls"] = images
    resp = await asyncio.wait_for(context.llm_generate(**kwargs), timeout=timeout)
    return getattr(resp, "completion_text", "") or str(resp)


def _image_to_part(path_or_url: str) -> str:
    if path_or_url.startswith(("http://", "https://", "data:")):
        return path_or_url
    ext = os.path.splitext(path_or_url)[1].lower().lstrip(".") or "png"
    if ext == "jpg":
        ext = "jpeg"
    with open(path_or_url, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode("ascii")
    return f"data:image/{ext};base64,{b64}"


async def _call_via_http(
    api_base: str,
    api_key: str,
    model: str,
    prompt: str,
    images: list[str],
    timeout: float,
) -> str:
    import aiohttp

    url = api_base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = url + "/chat/completions" if url.endswith("/v1") else url + "/v1/chat/completions"
    content: Any = prompt
    if images:
        content = [{"type": "text", "text": prompt}]
        for img in images[:2]:
            try:
                content.append(
                    {"type": "image_url", "image_url": {"url": _image_to_part(img)}}
                )
            except Exception:
                continue
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.3,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout_cfg = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
        async with session.post(url, json=body, headers=headers) as resp:
            payload = await resp.json(content_type=None)
    try:
        return str(payload["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError):
        return ""


async def learn_cards(
    *,
    context: Any,
    note: str,
    last_reply: str = "",
    images: Optional[list[str]] = None,
    quotes: Optional[list[str]] = None,
    provider_id: str = "",
    api_base: str = "",
    api_key: str = "",
    model: str = "",
    timeout: float = 15.0,
    card_max_chars: int = 120,
    min_confidence: float = 0.55,
) -> Optional[dict[str, Any]]:
    """返回 {"cards": [...], "confidence": float} 或 None。"""
    images = [i for i in (images or []) if i]
    prompt = _build_prompt(note, last_reply, bool(images), quotes or [])
    raw = ""
    try:
        if api_base and model:
            raw = await _call_via_http(
                api_base, api_key, model, prompt, images, timeout
            )
        elif provider_id:
            raw = await _call_via_provider(context, provider_id, prompt, images, timeout)
        else:
            return None
    except asyncio.TimeoutError:
        return None
    except Exception:
        return None

    data = _parse_json(raw)
    if not data or not data.get("is_teaching"):
        return None
    cards = _clean_cards(data, card_max_chars)
    if not cards:
        return None
    return {"cards": cards, "confidence": float(data.get("confidence") or 0.6)}


# --------------------------------------------------------------------- 渲染
def _field(card: Any, key: str) -> str:
    """卡片可能是 Card 对象，也可能是刚提炼出来的 dict。"""
    if isinstance(card, dict):
        return str(card.get(key) or "")
    return str(getattr(card, key, "") or "")


def render_card(card: Any, index: int) -> str:
    """把一张卡渲染成给模型看的对照示例。"""
    lines = [f"{index}."]
    scene = _field(card, "scene")
    if scene:
        lines[0] += f" 场景：{scene}"
    bad = _field(card, "bad")
    good = _field(card, "good")
    principle = _field(card, "principle")
    if bad:
        lines.append(f"   ✗ 别这么说：「{bad}」")
    if good:
        lines.append(f"   ✓ 该这么说：「{good}」")
    if principle:
        lines.append(f"   要点：{principle}")
    return "\n".join(lines)


def card_line(card: Any) -> str:
    """单行摘要，给 /教 列表 用。"""
    parts = []
    scene = _field(card, "scene")
    bad = _field(card, "bad")
    good = _field(card, "good")
    principle = _field(card, "principle")
    if scene:
        parts.append(f"[{scene}]")
    if bad:
        parts.append(f"✗{bad}")
    if good:
        parts.append(f"✓{good}")
    if principle:
        parts.append(f"要点:{principle}")
    return " ".join(parts)[:200] or "(空卡)"
