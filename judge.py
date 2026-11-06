"""教说话的识别：只负责「这条消息是不是在教」，不负责抽规则。

v1 的教训：用正则从人话里"抽"规则，抽出来的是碎片
（实例里那条 rule='么不发也行' 就是产物）。v2 改成
「识别意图 → 交给模型看原文/图片提炼说话卡」，本地只做便宜的初筛。
"""

from __future__ import annotations

import re
from typing import Optional

# 强信号：出现基本可以判定"在教"
STRONG_CUES = (
    "教你", "教教", "纠正", "说错", "说反", "改口", "换个说法", "换种说法",
    "以后别", "下次别", "以后不要", "不要再", "不许再", "别老是", "别总",
    "应该说", "要这么说", "应该这么", "应该这样", "该这么说", "记得以后",
    "记住", "记着", "给我记住", "你要学会", "学着点", "这样不对", "这么说不",
    "学学", "学一下", "照这个", "按这个来", "模仿", "别再这么回", "不要这么回",
    "口语点", "口语化", "说人话", "别端着",
)

# 弱信号：像在教，但需要模型再判一次
WEAK_CUES = (
    "不对", "不是这样", "不像是", "听着奇怪", "太生硬", "太官方", "像机器人",
    "ai味", "AI味", "人机", "机械", "书面", "说教", "语气", "措辞", "说法",
    "回应", "回答问题", "说话", "别用", "少用", "多用", "听起来", "感觉你",
    "你刚才", "上一条", "那句", "范例", "示例", "例子",
)

# 纯寒暄/命令式短句：连模型都不用问
CHAT_NOISE = (
    "在吗", "在不在", "晚安", "早安", "午安", "好的", "嗯", "哦", "哈", "哈哈",
    "收到", "谢谢", "拜拜", "睡了", "吃了吗", "干嘛呢", "在干嘛", "绷", "草",
)

POSITIVE_FEEDBACK = (
    "就是这样", "这就对了", "对了", "对的对的", "嗯对", "记住就好", "乖",
    "不错", "很好", "学得不错", "记住了", "有进步", "这个好", "这句好",
    "就要这样", "这种感觉", "对味",
)
NEGATIVE_FEEDBACK = (
    "还是不对", "又错了", "还是错", "不对吧", "你怎么又", "又来了",
    "没学会", "白教了", "还是没记住", "太机械", "人机味", "ai味", "AI味",
    "太官方", "太生硬", "像机器人", "说教", "背书", "别这么回", "不要这么回",
)

_CMD_PREFIX = ("/", "!", "。")

# 引号包裹的原文，v2 只用来给模型"提示重点"，不做抽取
_QUOTE_RE = re.compile(r"[「“\"']([^」”\"']{1,200})[」”\"']")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def is_command(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith(_CMD_PREFIX)


def detect(text: str, *, has_image: bool = False) -> str:
    """返回 'strong' / 'possible' / 'chat'。带图的消息至少进 possible。"""
    t = _norm(text)
    if not t and not has_image:
        return "chat"
    if t and len(t) <= 6 and any(t == w or t.startswith(w) for w in CHAT_NOISE):
        return "chat"
    low = t.lower()
    if any(cue.lower() in low for cue in STRONG_CUES):
        return "strong"
    if len(t) >= 6 and any(cue.lower() in low for cue in WEAK_CUES):
        return "possible"
    if has_image:
        return "possible"
    return "chat"


def detect_feedback(text: str) -> Optional[bool]:
    """True=夸（正面），False=嫌（负面），None=不是反馈。"""
    t = _norm(text)
    if not t or len(t) > 60:
        return None
    low = t.lower()
    # 负向优先：像"也不是不对吧"这种，宁可当没学会
    if any(w.lower() in low for w in NEGATIVE_FEEDBACK):
        return False
    if any(w.lower() in low for w in POSITIVE_FEEDBACK):
        return True
    return None


def quoted(text: str) -> list[str]:
    return [m.group(1).strip() for m in _QUOTE_RE.finditer(text or "") if m.group(1).strip()]
