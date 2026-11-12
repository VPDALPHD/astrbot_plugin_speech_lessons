"""教说话的识别：只负责「这条消息是不是在教」，不负责抽规则。

v1 的教训：用正则从人话里"抽"规则，抽出来的是碎片
（实例里那条 rule='么不发也行' 就是产物）。v2 改成
「识别意图 → 交给模型看原文/图片提炼说话卡」，本地只做便宜的初筛。
"""

from __future__ import annotations

import re

STRONG_CUES = (
    "换个说法",
    "换种说法",
    "要这么说",
    "你应该说",
    "你可以这么回",
    "该这么说",
    "别再这么回",
    "别这么回",
    "不要这么回",
    "口语点",
    "口语化",
    "说人话",
    "别端着",
)

WEAK_CUES = (
    "太生硬",
    "太官方",
    "像机器人",
    "ai味",
    "人机味",
    "太机械",
    "太书面",
    "太像客服",
    "像在接工单",
    "别说教",
    "不要说教",
)

_STYLE_SUBJECTS = (
    "语气",
    "口气",
    "口吻",
    "措辞",
    "说法",
    "说话方式",
    "怎么接话",
    "这样说话",
    "这么说话",
    "这样回",
    "这么回",
    "接话",
    "反问",
    "说教",
)
_STYLE_REQUESTS = (
    "别",
    "不要",
    "少用",
    "多用",
    "改",
    "学",
    "模仿",
    "应该",
    "以后",
    "下次",
)
_EXAMPLE_REQUESTS = {
    "学学",
    "学一下",
    "学学这个",
    "学学这句",
    "学学这个范例",
    "学学这张图",
    "学学这张截图",
    "照这个学",
}
# 纯寒暄/命令式短句：连模型都不用问
CHAT_NOISE = (
    "在吗",
    "在不在",
    "晚安",
    "早安",
    "午安",
    "好的",
    "嗯",
    "哦",
    "哈",
    "哈哈",
    "收到",
    "谢谢",
    "拜拜",
    "睡了",
    "吃了吗",
    "干嘛呢",
    "在干嘛",
    "绷",
    "草",
)

POSITIVE_FEEDBACK = (
    "就是这样",
    "这就对了",
    "对了",
    "对的对的",
    "嗯对",
    "记住就好",
    "乖",
    "不错",
    "很好",
    "学得不错",
    "记住了",
    "有进步",
    "这个好",
    "这句好",
    "就要这样",
    "这种感觉",
    "对味",
)
NEGATIVE_FEEDBACK = (
    "还是不对",
    "又错了",
    "还是错",
    "不对吧",
    "你怎么又",
    "又来了",
    "没学会",
    "白教了",
    "还是没记住",
    "太机械",
    "人机味",
    "ai味",
    "AI味",
    "太官方",
    "太生硬",
    "像机器人",
    "说教",
    "背书",
    "别这么回",
    "不要这么回",
)

_CMD_PREFIX = ("/", "!", "。")

# 引号包裹的原文，v2 只用来给模型"提示重点"，不做抽取
_QUOTE_RE = re.compile(r"[「『“\"']([^」』”\"']{1,200})[」』”\"']")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def is_command(text: str) -> bool:
    t = (text or "").strip()
    return t.startswith(_CMD_PREFIX)


def is_bare_example_request(text: str) -> bool:
    """Check whether the user asks to learn without supplying an example in text."""
    return _norm(text).strip("。！!？? ") in _EXAMPLE_REQUESTS


def detect(text: str, *, has_image: bool = False) -> str:
    """返回 'strong' / 'possible' / 'chat'。带图的消息至少进 possible。"""
    t = _norm(text)
    if not t and not has_image:
        return "chat"
    if t and len(t) <= 6 and any(t == w or t.startswith(w) for w in CHAT_NOISE):
        return "chat"
    low = t.lower().replace("ai 味", "ai味")
    quoted_instruction = re.search(
        r"""(?:别(?:再|总)?[回说用]|不要(?:再)?[回说用]|应该说)\s*[「『“"']""", t
    )
    if (
        any(cue in low for cue in STRONG_CUES)
        or is_bare_example_request(t)
        or quoted_instruction
    ):
        return "strong"
    if any(cue in low for cue in WEAK_CUES) or any(
        any(cue in clause for cue in _STYLE_REQUESTS)
        and any(subject in clause for subject in _STYLE_SUBJECTS)
        for clause in re.split(r"[。！？!?；;\n]", low)
    ):
        return "possible"
    if has_image:
        return "possible"
    return "chat"


def detect_feedback(text: str) -> bool | None:
    """True=夸（正面），False=嫌（负面），None=不是反馈。"""
    t = _norm(text)
    if not t or len(t) > 60:
        return None
    low = t.lower().replace("ai 味", "ai味")
    # 负向优先：像"也不是不对吧"这种，宁可当没学会
    if any(w.lower() in low for w in NEGATIVE_FEEDBACK):
        return False
    if any(w.lower() in low for w in POSITIVE_FEEDBACK):
        return True
    return None


def quoted(text: str) -> list[str]:
    return [
        m.group(1).strip() for m in _QUOTE_RE.finditer(text or "") if m.group(1).strip()
    ]
