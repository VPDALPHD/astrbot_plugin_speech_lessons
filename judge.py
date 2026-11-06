"""教说话的识别与抽取：规则优先，模型兜底。

三档判定：
- strong：明显在教（命中强信号词）→ 本地正则直接抽，零模型调用；
- possible：像在教但说不准 → 交给模型判定；
- chat：普通聊天 → 直接放过，不烧钱。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# 强信号：出现基本可以判定"在教"
STRONG_CUES = (
    "教你", "教教", "纠正", "说错", "说反", "改口", "换个说法", "换种说法",
    "以后别", "下次别", "以后不要", "不要再", "不许再", "别老是", "别总",
    "应该说", "要这么说", "应该这么", "应该这样", "该这么说", "记得以后",
    "记住", "记着", "给我记住", "你要学会", "学着点", "这样不对", "这么说不",
)

# 弱信号：可能是在教，需要模型再判一次
WEAK_CUES = (
    "不对", "不是这样", "不像是", "听着奇怪", "太生硬", "太官方", "像机器人",
    "语气", "措辞", "说法", "回应", "回答问题", "说话", "别用", "少用", "多用",
    "听起来", "感觉你", "你刚才", "上一条", "那句",
)

# 纯寒暄/命令式短句：连模型都不用问
CHAT_NOISE = (
    "在吗", "在不在", "晚安", "早安", "午安", "好的", "嗯", "哦", "哈", "哈哈",
    "收到", "谢谢", "拜拜", "睡了", "吃了吗", "干嘛呢", "在干嘛",
)

POSITIVE_FEEDBACK = (
    "就是这样", "这就对了", "对了", "对的对的", "嗯对", "记住就好", "乖",
    "不错", "很好", "学得不错", "记住了", "有进步",
)
NEGATIVE_FEEDBACK = (
    "还是不对", "又错了", "还是错", "不是这样", "不对吧", "你怎么又", "又来了",
    "没学会", "白教了", "还是没记住",
)

_QUOTE_RE = re.compile(r"[「“\"']([^」”\"']{1,120})[」”\"']")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def detect(text: str) -> str:
    """返回 'strong' / 'possible' / 'chat'。"""
    t = _norm(text)
    if not t:
        return "chat"
    if len(t) <= 6 and any(t == w or t.startswith(w) for w in CHAT_NOISE):
        return "chat"
    if any(cue in t for cue in STRONG_CUES):
        return "strong"
    if len(t) >= 8 and any(cue in t for cue in WEAK_CUES):
        return "possible"
    return "chat"


def detect_feedback(text: str) -> Optional[bool]:
    """判断这条是不是在评价上一条规则：True=正向，False=负向，None=不是反馈。"""
    t = _norm(text)
    if not t or len(t) > 40:
        return None
    if any(w in t for w in NEGATIVE_FEEDBACK):
        return False
    if any(w in t for w in POSITIVE_FEEDBACK):
        return True
    return None


def _quoted(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(text or "") if m.group(1).strip()]


def extract_by_rules(text: str) -> list[dict[str, Any]]:
    """本地正则抽取，抽不到返回空列表（交给模型兜底）。"""
    t = _norm(text)
    out: list[dict[str, Any]] = []
    quotes = _quoted(t)

    # 1) 别说 X，要说 Y / 不要说 X，应该说 Y
    m = re.search(
        r"(?:别|不要|不许|别老|别总)(?:说|讲|用|回)?\s*[「“\"']?(.{1,40}?)[」”\"']?\s*[，,。;；]\s*"
        r"(?:应该|要|可以|就|改成|换成|以后)(?:说|讲|用|回)?\s*[「“\"']?(.{1,80}?)[」”\"']?\s*$",
        t,
    )
    if m:
        wrong, rule = m.group(1).strip(), m.group(2).strip()
        if rule and len(rule) >= 2:
            out.append({"rule": rule, "wrong": wrong, "confidence": 0.85})

    # 2) 应该说 / 要这么说 / 应该这样回 + 引号内容
    if not out:
        m = re.search(
            r"(?:应该|要|可以|下次|以后)(?:这么说|这样说|这么回|这样回|说|讲|回)\s*[：:，,]?\s*(.+)$",
            t,
        )
        if m:
            body = m.group(1).strip()
            if body:
                out.append({"rule": body[:200], "wrong": "", "confidence": 0.7})

    # 3) 记住 / 记着：后面的内容
    if not out:
        m = re.search(r"(?:记住|记着|给我记住)\s*[：:，,]?\s*(.+)$", t)
        if m:
            body = m.group(1).strip()
            if len(body) >= 2:
                out.append({"rule": body[:200], "wrong": "", "confidence": 0.75})

    # 4) 别老是 / 不要老 说 X（没有"应该"的部分）→ 折成"不要老说 X"
    if not out:
        m = re.search(
            r"(?:以后|下次|记住)?\s*(?:别|不要|不许|少)(老是|老|总|总是)?\s*(?:说|讲|用|回)?\s*[「“\"']?(.{2,60}?)[」”\"']?"
            r"(?:[，,。;；！!]|$)",
            t,
        )
        if m:
            body = m.group(2).strip()
            if body and len(body) >= 2:
                out.append({"rule": f"不要老说{body}"[:200], "wrong": body[:80], "confidence": 0.6})

    # 5) 只有引号没动词：保守地认为第一个引号是正确说法
    if not out and quotes and len(t) <= 120:
        out.append({"rule": quotes[-1][:200], "wrong": "", "confidence": 0.4})

    # 6) 都没有、但确实明显在教：把整句当规矩收下（零模型成本）
    if not out and len(t) <= 150 and any(cue in t for cue in STRONG_CUES):
        out.append({"rule": t[:200], "wrong": "", "confidence": 0.45})

    # 去掉"教："这类前缀残留，剥掉包裹的引号
    for item in out:
        rule = re.sub(r"^(?:教你|教教|我说|就是说)[：:，,\s]+", "", item["rule"]).strip()
        rule = rule.strip("「」“”\"'‘’ ")
        item["rule"] = rule.strip()
    return [item for item in out if item["rule"]]


JUDGE_PROMPT = """判断下面这条消息，说话人是不是在"教 AI 怎么说话/怎么回应"，也就是在纠正它的表达方式或给它立规矩。

是 → 输出规则；不是 → is_teaching 为 false。
注意：单纯提问、闲聊、陈述事实都不是教学；但"以后别这么回""这样说太生硬了""你应该先问他感受"这类都算。

只输出 JSON，不要解释：
{"is_teaching": true/false, "confidence": 0.0-1.0,
 "rule": "以后应该怎么做（一句祈使句，不超过80字）",
 "wrong": "不该怎么做（没有就留空）",
 "tags": ["风格"/"称呼"/"语气"/"内容"/"边界"]}

消息：
{text}
"""


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


async def judge_with_model(
    context: Any,
    provider_id: str,
    text: str,
    timeout: float = 8.0,
) -> Optional[dict[str, Any]]:
    """模型兜底判定 + 抽规则。失败一律返回 None（宁可不学，也不要乱学）。"""
    import asyncio

    prompt = JUDGE_PROMPT.replace("{text}", (text or "")[:800])
    try:
        resp = await asyncio.wait_for(
            context.llm_generate(chat_provider_id=provider_id, prompt=prompt),
            timeout=timeout,
        )
    except Exception:
        return None
    raw = getattr(resp, "completion_text", "") or str(resp)
    data = _parse_json(raw)
    if not data:
        return None
    if not data.get("is_teaching"):
        return None
    rule = str(data.get("rule") or "").strip()
    if not rule or len(rule) > 200:
        return None
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return {
        "rule": rule,
        "wrong": str(data.get("wrong") or "").strip()[:200],
        "confidence": max(0.0, min(1.0, confidence)),
        "tags": [str(t)[:12] for t in tags if str(t).strip()][:5],
    }
