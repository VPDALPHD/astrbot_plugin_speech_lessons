# 「教我说话」外部联动接口手册

> 适用版本：astrbot_plugin_speech_lessons **2.4.0+**（本机补丁版）
> 目的：让其他 AstrBot 插件在自己的生成链路里复用本插件的「人类伪装指南 + 说话卡」，
> 并把自己的教学材料交给本插件提炼成说话卡。
> 本文所有接口都是**同进程 Python 调用**（不是 HTTP 协议），只暴露纯数据与渲染结果。

---

## 1. 能拿到什么（三个能力）

| 能力 | 作用 | 典型用途 |
|---|---|---|
| 说话风格块 | 一段可直接拼进 prompt 的文本：「人类伪装指南」+ 若干张「场景 ✗反例 → ✓正例」说话卡 | 生成评论/回复/动态/私信前注入 |
| 选卡数据 | 结构化的卡片列表（场景/反例/正例/原则/权重） | 想自己拼 prompt、或做面板展示 |
| 学习入口 | 把「优秀范例 + 主人原话」交过来，提炼成说话卡（顺带把通用准则写进指南） | 看完视频/读完长文后学它的表达方式 |

设计取舍：**不传宿主对象**（不交出 AstrBot event、插件实例、数据库连接），调用方只依赖 DTO 与文本。

---

## 2. 如何发现本插件

AstrBot 没有跨插件 import 约定，用 duck typing 发现：

```python
def find_speech_lessons_api(context):
    """在当前已加载的插件里找「教我说话」的只读门面；找不到返回 None。"""
    get_all_stars = getattr(context, "get_all_stars", None)
    if not callable(get_all_stars):
        return None
    for star in get_all_stars() or []:
        if str(getattr(star, "name", "") or "") != "astrbot_plugin_speech_lessons":
            continue
        instance = getattr(star, "star_cls", None) or getattr(star, "instance", None)
        getter = getattr(instance, "public_api", None)
        if not callable(getter):
            continue
        try:
            api = getter()
        except Exception:
            continue
        return api if isinstance(api, dict) else None
    return None
```

要点：
- `public_api` 是**方法**（要调用一次），返回 dict；`api_version` 当前为 `1`。
- 建议缓存 60~120 秒，并在插件重载后失效（重载会换实例）。示例实现见
  `astrbot_plugin_bili_scroll/core/speech_bridge.py`。
- 拿不到就**静默降级**（不注入、不报错），别让联动拖垮你的主流程。

---

## 3. 接口详情

### 3.1 `enabled() -> bool`

本插件是否启用（总开关 + 运行时开关）。返回 False 时其余接口都会给空结果，可以不调。

### 3.2 `pick_cards(text: str) -> list[dict]`

按场景挑卡（内部：权重 + 复习到期 + 场景词命中 + 语义相似度打分，取前 N）。

```python
[{"id": 12, "scene": "对方用梗调侃你时", "bad": "难绷啥呢",
  "good": "绷不住了", "principle": "接梗式短句，别反问", "weight": 2.1}]
```

- 只读，不会推进复习、不写库。
- 卡片数量与字数受本插件配置（`inject_max_cards` / `inject_max_chars`）约束。

### 3.3 `render_block(text: str, *, max_chars: int = 0) -> tuple[str, list[int]]`

**最常用**。返回 `(block, 用到的卡片 id 列表)`：

- `block` 是可直接拼进提示词的文本，**已经排好序**：
  1. `【人类伪装指南 · 我的指南书】` + 最近几天的记录（每次必在，除非被配置关闭）
  2. `【说话卡参考 · 主人在私聊里教的口气示范】` + 若干「✗反例 / ✓正例 / 要点」条目
- **同步函数**（内部不 await），放在 async 代码里直接调，不要 `await`。
- **只读最近 `guide_inject_days` 天**，不翻旧页。要翻旧页用下面的异步版。
- 没有任何内容时返回 `("", [])`，调用方据此跳过注入即可。
- `max_chars` 只约束**说话卡**部分的预算；指南长度由本插件 `guide_max_chars` 决定。
- 调用会顺带 `touch` 用到的卡片（用于闲置衰减计数），不影响正确性。

### 3.3b `await render_block_async(text: str, *, max_chars: int = 0) -> tuple[str, list[int]]`

调用方是 async 时**优先用它**——在同步版基础上多一层「翻旧页」：先按当前文本切出关键词去搜旧页
（二字滑窗），没命中再用向量召回（`guide_recall_days` / `guide_recall_threshold`）。
指南书预算 `guide_max_chars`（默认 8000）会被整段带入，`max_chars` 仍然只裁说话卡。
异常时自动退回同步版，不会抛给调用方。

### 3.4 `await learn_from_note(note, *, umo="", source="external") -> dict | None`

提交带有交谈场景和回复原话的范例：

```python
result = await api["learn_from_note"](
    "对方说：我今天真是气死了。\n认可的回复：出什么事了？你慢慢说。\n教学意图：学学这个接话方式。",
    umo="aiocqhttp:FriendMessage:123456",   # 用于解析该会话的默认模型，可留空
    source="external",
)
# 成功: {"cards": [...], "count": 3, "guide_notes_added": 1}
# 失败: None（模型不可用 / 置信度不足 / 无可提炼内容）
```

行为说明：
- 与本插件「主人私聊教一次」走**同一条提炼链**（模型提炼 → 核验引用与置信度 → 卡片上限 → 判重）。仅包含生成摘要的 B 站点播和本地视频联动会返回 `None`。
- 材料里那些「不适合做某场景说话卡、但属于怎么说话才像人」的通用准则，会被顺带写进**人类伪装指南**
  （每次回复都注入），所以一次调用可能同时产出卡片和指南条目。
- 幂等性由「卡片指纹 + 向量相似度」保证：重复教同一件事只会加权重，不会重复建卡。
- 该接口**不会**校验调用方身份，请只在可信语境调用（例如主人私聊、或你确认过的教学场景）。

---

## 4. 推荐调用姿势

```python
block, ids = ("", [])
api = find_speech_lessons_api(self.context)
if api is not None:
    try:
        block, ids = api["render_block"](prompt_or_scene_text, max_chars=900)
    except Exception:
        block, ids = "", []      # 任何异常都降级，不影响本轮生成
if block:
    system_prompt = f"{system_prompt}\n\n{block}"
```

- 一轮只调一次，放在真正生成的那次模型调用前。
- `text` 传「本轮要回应的原文/场景」，挑选会更准（传空串也能用，只是退化成按权重取卡）。
- 你的 prompt 里最好补一句「这些约束只影响措辞，不要改变输出格式或 JSON 结构」，避免结构化输出被带偏。

---

## 5. 降级与边界

| 情况 | 表现 |
|---|---|
| 本插件未安装/未启用 | `find_...()` 返回 None；`render_block` 不会报「不存在」 |
| 说话卡为空、指南为空 | 返回 `("", [])` |
| 向量模型不可用 | 判重与选卡自动退回字面/权重逻辑，接口照常返回 |
| 模型不可用（learn） | 返回 None，不会抛异常 |
| 超过卡片/字数上限 | 按配置截断或拒绝新增 |

调用方建议：**所有联动调用都包 try/except**，并把失败当正常分支处理。

---

## 6. 版本与兼容

- `api_version` 目前为 `1`；破坏性变更会升主版本号并改方法签名。
- 自 v2.5.0 起 `public_api()` 随插件包一起发布，升级不会被覆盖。
  调用方仍建议对「找不到 api / 缺某个 key」做兼容分支。
- 依赖的宿主能力：`req.extra_user_content_parts` + `mark_as_temp()`（AstrBot ≥ 4.24.2）是本插件
  自己注入用的；**你的插件不受此限制**，只需要能拿到 `public_api`。

---

## 7. 面板 HTTP 接口（仅供本插件面板，不建议外部依赖）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/{plugin}/cards` | 列出说话卡 |
| POST | `/{plugin}/cards/save` | 新建/修改卡片 |
| POST | `/{plugin}/cards/<id>/archive|restore|delete` | 归档/恢复/删除 |
| GET | `/{plugin}/config` | 配置项字段表 + 当前值 + 平台模型列表 |
| POST | `/{plugin}/config/save` | 保存配置（白名单键） |
| POST | `/{plugin}/embedding/test` | 测试向量接口 |
| GET | `/{plugin}/embedding/providers` | 平台 embedding / chat 模型列表 |
| GET | `/{plugin}/guide` | 读人类伪装指南 |
| POST | `/{plugin}/guide/save` | 存人类伪装指南 |
| POST | `/{plugin}/guide/review` | 立即精炼指南（用当天对话） |

这些接口的参数与返回**可能随版本变化**，跨插件请优先用 Python 的 `public_api`。

---

## 8. 隐私与安全

- 只读接口（`pick_cards` / `render_block`）不返回任何用户隐私字段，不触发模型调用。
- 指南与说话卡的内容会出现在**你注入的那次模型请求**里；如果那个会话是公开场景（群聊），
  请自行确认内容适合公开（本插件指南里禁止写具体人名/事实/隐私，但你不能假设一定干净）。
- `learn_from_note` 会调用一次模型；不要把它放在高频路径上（建议加冷却或去重）。

---

## 9. FAQ

**Q：为什么 `render_block` 返回了内容，但模型好像没遵守？**
A：它在注入链里属于「用户侧参考」，不是系统级硬约束。想更强，可把 `block` 放 system_prompt 末尾，
或提高你的提示词优先级；本插件自身建议放「参考」位置。

**Q：能只拿说话卡、不要指南吗？**
A：`pick_cards()` 只返回卡片数据；`render_block()` 总是先带上指南。

**Q：能读到主人教过的原始教学文本吗？**
A：不能，接口只给提炼后的卡片结构，不返回原始聊天内容。
