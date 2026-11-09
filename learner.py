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

是 → 提炼「说话卡」，最多 <<MAX_CARDS>> 张。严格要求：
1. 先找出主人这次在纠正或示范的说话方式。bad 和 good 优先摘录能说明差别的最短原话。原话可以来自主人明确指出的文字、被指出的上一轮回复，或主人让你学的材料。保留措辞，不润色，不补写台词。
2. 主人明确点名一句旧回复时，把被点名的原文保留在 bad 里。主人指出的是一种说话动作时，例如「别急着分析」，bad 可以写成短语，例如"对方刚开始吐槽就分析原因"。涉及事情本身的操作要求，按原意理解。
3. 主人指出了问题、但没有给出或认可一句示范时，good 留空。有明确的接话方向，就把它写进 principle。例如，主人说「别回『收到』，像在接工单」，bad 填"收到"，good 留空，principle 填"闲聊里直接接对方刚说的事"。
4. 主人只给了明确认可或要求学习的示范时，good 摘录范例原话，bad 可以留空。
5. 同一次纠正里既有被指出的旧说法，又有主人明确给出的新说法时，把两句放进同一张卡。方向或示范无法从材料确定时，允许留空。
6. scene 写清这张卡的使用时机。优先写对方说了什么、当时是什么情绪或场合。材料里有能辨认场景的短句时，可以保留原话。例："对方说『我都快气死了』，是在吐槽刚发生的事。" 适用范围很广的卡也写清触发条件。
7. principle 写一句当场就能照做的接话动作，不超过 50 字。例："先接住她在气什么，等她问怎么办再给建议。"
8. tags 填 1 到 4 个能区分这张卡的短词，优先用聊天中可能出现的说法，例如"气死了"、"吐槽"、"求建议"。
9. 图片可能是聊天截图：把截图里主人的纠正和合格回复的关键原话抄进 bad / good。
10. 宁少勿滥；但材料里确实有多条可迁移的说法时，每条各出一张卡，不要全部压成一张。
11. 如果材料是一段视频/长文的内容描述（不是主人当面的纠正）：把里面**每一条可复用的表达方式或句式**各出一张卡
    （例如怎么开场、怎么接梗、怎么吐槽、怎么讲道理），scene 填适用的评论区/聊天场景，bad 可留空。

只输出 JSON，不要解释、不要 markdown：
{"is_teaching": true, "confidence": 0.0-1.0,
 "cards": [{"scene": "", "bad": "", "good": "", "principle": "", "tags": ["语气"], "confidence": 0.0-1.0}],
 "guide_notes": ["人类……"]}

额外字段 guide_notes：材料里如果有**不适合做成说话卡**、但属于「人类怎么说话、怎么反应」的观察，
写成一句以「人类」开头的观察笔记；需要时可补一句极简的“我可以/我不能”。
例：“人类被夸的时候往往先说‘哪有’，然后才笑。”或“人类聊天不把话说完时，我可以选择不用接。”
最多 3 条；没有就给空数组。不要写口号、不要写具体人名，也不要重复 cards 里已有的内容。
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


def _build_prompt(
    note: str,
    last_reply: str,
    has_image: bool,
    quotes: list[str],
    *,
    max_cards: int = 3,
    note_max_chars: int = 800,
) -> str:
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
        LEARN_PROMPT.replace("<<MAX_CARDS>>", str(max(1, int(max_cards))))
        .replace("<<NOTE>>", (note or "")[: max(200, int(note_max_chars))])
        .replace("<<LAST_REPLY>>", last_reply_block)
        .replace("<<IMAGE>>", image_block)
        .replace("<<QUOTED>>", quoted_block)
    )


def _clean_cards(
    data: dict[str, Any], card_max_chars: int, max_cards: int = 3
) -> list[dict[str, Any]]:
    cards = data.get("cards")
    if not isinstance(cards, list):
        return []
    out: list[dict[str, Any]] = []
    for raw in cards[: max(1, int(max_cards))]:
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
    max_cards: int = 3,
    note_max_chars: int = 800,
) -> Optional[dict[str, Any]]:
    """返回 {"cards": [...], "confidence": float} 或 None。"""
    images = [i for i in (images or []) if i]
    prompt = _build_prompt(
        note,
        last_reply,
        bool(images),
        quotes or [],
        max_cards=max_cards,
        note_max_chars=note_max_chars,
    )
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
    cards = _clean_cards(data, card_max_chars, max_cards)
    notes = _clean_guide_notes(data)
    if not cards and not notes:
        return None
    return {
        "cards": cards,
        "confidence": float(data.get("confidence") or 0.6),
        "guide_notes": notes,
    }


def _clean_guide_notes(data: dict[str, Any], *, max_items: int = 3, max_chars: int = 120) -> list[str]:
    """从提炼结果里取「适合写进人类伪装指南」的通用准则（去重、限长、限条数）。"""
    raw = data.get("guide_notes")
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        text = text[:max_chars].strip()
        if len(text) < 4:
            continue
        if any(text in existing or existing in text for existing in out):
            continue
        out.append(text)
        if len(out) >= max_items:
            break
    return out


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
        lines.append(f"   ✗ 当时不合适的说法：「{bad}」")
    if good:
        lines.append(f"   ✓ 当时合适的说法：「{good}」")
    if principle:
        lines.append(f"   相似场合怎么接：{principle}")
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
