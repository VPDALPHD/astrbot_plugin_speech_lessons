# 教我说话

![封面](logo.png)

> 作者：**VPDALPHD，DS_CHAN** ｜ 许可证 MIT ｜ 仓库 <https://github.com/VPDALPHD/astrbot_plugin_speech_lessons>
> 版本历史见 [CHANGELOG.md](CHANGELOG.md)（也显示在面板「插件详情 → 更新日志」）

## 这是什么

私聊里用一句「这样不对，AI 味太重」把你的说话方式教给 AI；再给它一本会自己续写的
**《人类伪装指南》**，让它长期记住"人类怎么说话"。

- **说话卡**：场景 / 反例原文 / 正例原文 / 一句要点，由模型从你的原话提炼，下一轮就能用。
- **人类伪装指南**：按天写的观察笔记，一天一页，每轮优先注入；可翻页、可编辑、可划掉。
- **看图学**：聊天截图、优秀范例截图都能学（需模型支持图片输入）。
- **真复习**：你说"对/就是这样"才推进间隔，说"不对"就降权；久不用的卡自动淡忘归档。

实现要点：私聊钩子挂 250000 优先级（早于陪伴插件的 `stop_event`）+ 9 秒防抖合并；
提炼照抄原文不润色；写入前向量判重；注入走 `extra_user_content_parts` 临时片段，
不动 `system_prompt`；数据全部落本地 SQLite，无第三方依赖。

## 面板可视化视图

插件自带一个管理面板，重载插件后出现在 WebUI 左侧 **「扩展功能 → 插件视图 → 教我说话」**
（也可以从 插件管理 → 教我说话 → 视图 进入）。

- **看**：卡片列表，显示场景 / ✗ 反例 / ✓ 正例 / 要点 / 标签 / 权重 / 注入次数 / 复习次数 / 创建时间
- **查**：搜索框（场景、反例、正例、要点全文模糊匹配）+ 状态筛选（生效中 / 已归档 / 全部）
- **增**：右上角「＋ 新建卡片」，手动写一张
- **改**：每张卡「编辑」，改完下一轮对话立刻生效（改 scene/bad/good 会重算去重指纹）
- **删**：先「归档」（可恢复，等同聊天里的 `/教 删 N`），再点一次「删除」才真正抹掉（4 秒内二次确认）
- 顶部统计：生效中 / 已归档 / 注入总次数 / 平均权重 / 反馈 👍👎

实现方式就是官方那套：`views/说话卡/` + `context.register_web_api()`，页面通过
`window.AstrBotPluginView` bridge 调后端，跟随面板深浅色主题。

后端接口（都在 `/astrbot_plugin_speech_lessons/` 下）：

| 路由 | 方法 | 用途 |
|---|---|---|
| `cards` | GET | 列卡（`status` / `q` / `limit`）+ 统计 |
| `cards/save` | POST | 新建或修改（有 `id` 就是改） |
| `cards/<id>/archive` | POST | 归档 |
| `cards/<id>/restore` | POST | 恢复 |
| `cards/<id>/delete` | POST | 彻底删除 |
| `config` | GET | 配置项字段表 + 当前值 + 平台模型列表 |
| `config/save` | POST | 保存配置（白名单键） |
| `embedding/test` | POST | 测试向量接口 |
| `embedding/providers` | GET | 平台 embedding / chat 模型列表 |
| `guide` | GET | 读「人类伪装指南」（兼容旧接口） |
| `guide/save` | POST | 保存「人类伪装指南」（兼容旧接口） |
| `journal/page` | GET | 读某一天的书页 |
| `journal/save` | POST | 保存某一天的书页 |
| `journal/search` | GET | 关键词翻旧页 |
| `journal/write_today` | POST | 写/重写今天 |
| `journal/toggle` | POST | 开关指南注入 |
| `journal/delete` | POST | 删除某一天 |

## 给其他插件用：外部联动接口（v2.4.0+）

本插件对外暴露一个只读门面 `public_api()`，别的插件可以用它复用「人类伪装指南 + 说话卡」，
或者把教学材料交给本插件提炼。**完整接口手册见 [docs/EXTENSION_API.md](docs/EXTENSION_API.md)**。

```python
api = find_speech_lessons_api(context)          # 见手册里的发现代码
block, ids = api["render_block"](scene_text)    # 同步调用，返回可直接拼进 prompt 的文本
```

一句话总结：
- `render_block(text)` → `(指南 + 说话卡的文本块, 用到的卡 id)`，没有内容返回 `("", [])`；
- `pick_cards(text)` → 结构化卡片列表；
- `await learn_from_note(note)` → 把优秀范例提炼成说话卡（顺带把通用准则写进指南）。

## 兼容性测试

已实测可与下列插件**同时启用**。表内名称是 **AstrBot 面板里显示的名字**（不是仓库名），
版本为测试时实际运行的版本；测试环境 AstrBot 4.22 + aiocqhttp（NapCat），共 16 个插件同时启用。

| 面板显示名 | 版本 | 关系 | 结论 |
|---|---|---|---|
| 我会永远陪着你 | 6.5.6 | 它 `priority=0/220000` 处理私聊并 `stop_event()` 接管整轮；消息分发遇 stopped 会 break | ✅ 已适配：本插件捕获钩子挂 **250000**，排在它之前 |
| 我会牢牢记住你 | 1.10.5 | `on_llm_request(-20)` 注入记忆；工具 `memory_companion_remember` | ✅ 兼容：双方都是 append system_prompt，互不覆盖；工具描述已划清"记事实 vs 学口气" |
| 思维链式消息防抖 | 1.4.2 | 在 `on_waiting_llm_request` 才合并消息，晚于消息处理器 | ✅ 已适配：本插件自带 9 秒防抖缓冲（`capture_debounce_seconds`） |
| 表情包管理器 | 5.3.3 | `on_llm_request(99999)` 剥自己的提示块 | ✅ 兼容：时序在本插件之前 |
| 表情包收藏库 | 1.7.0 | 4 个工具 + `on_llm_response` / `after_message_sent` | ✅ 兼容：不碰 system_prompt |
| 大肥鱼 · 活人感 | v0.4.0 | 消息处理器 `sys.maxsize-1`、回复钩子同优先级 | ✅ 兼容：不 stop 私聊 |
| 大肥鱼心情 | v0.3.0 | `on_llm_request` 追加心情 | ✅ 兼容：append 式 |
| 我会自己刷B站 | 1.9.7 | 私聊 850 消息处理器 | ✅ 兼容 |
| 万能解析器 | v1.6.1 | `parse_media_url` 工具 | ✅ 兼容：工具名不撞 |
| 群典 | 2.0.0 | 只发梗图 | ✅ 兼容 |
| 我会替你留住故事 | 0.2.2 | `content_companion_view_work` 工具 | ✅ 兼容 |
| 主动消息 (Proactive Chat) | v1.3.0-beta.1 | 私聊 999 / `after_message_sent` | ✅ 兼容 |
| QQ 消息内容归一化 | 1.0.1 | 消息标准化 | ✅ 兼容 |
| QQ 能力增强 | 1.15.2 | 29 个 `qq_*` 工具 + `on_llm_request(-20000)` | ✅ 兼容：不写 system_prompt（工具数量多，注意模型选择成本） |
| astrbot_plugin_blacklist_tools | v1.6 | 消息处理器 `sys.maxsize-1`（该插件无中文名，面板按包名显示） | ✅ 兼容：只拦被拉黑用户 |

**审计方法**：读 AstrBot 的 handler 注册/分发源码（`star_handler.py` 按 priority 降序、
`star_request.py` 遇 `stop_event()` 即 break），再逐插件扫 `llm_tool` 名、钩子优先级、
`req.system_prompt` 写法与 `stop_event` 调用点。完整报告见仓库外文档《教我说话v2-冲突审计》。


## 它解决什么问题

群里它冒出一句人机味很重的话，你私聊它说"这样不对，AI 味太重"，或者
丢一张聊天截图/同类优秀回复说"学学"。这类东西进不了长期记忆：
它是"行为口气"，不是"发生过的事实"，而且必须跨会话生效（私聊教的，群里要用）。

v1.0.0 的做法是从你这句人话里用正则"抽规则"。实际跑下来抽出来的是碎句子
（实测库里存成 `rule=么不发也行`，还注入了 96 轮，等于每轮喂给模型一句谜语）。
v2 换成：**识别意图 → 交给模型看原话/图片提炼「说话卡」→ 注入成对照示例**。

## 一次教学怎么走

```
你: [截图] 这样不对，AI 味太重了
        │
        ├─ 私聊钩子（priority=250000，比陪伴插件更早看到消息）   ← 兜底通道
        │     └─ 进防抖缓冲 9 秒（图 + 补充文字会被合并）
        └─ 她自己也判断 → 调 learn_speech_style(note=你的原话)     ← 主通道
                │
                ▼  同一句话只学一次（claim key 去重，240 秒窗口）
        模型提炼（可看图） → 1~3 张说话卡 → 写进 cards 表
                │
                ▼  下一轮
        system_prompt 里多一段：
        【说话卡】
        1. 场景：对方说"难绷"在抱怨时
           ✗ 别这么说：「难绷啥呢」
           ✓ 该这么说：「绷不住了」
           要点：接梗式短句，别反问，别解释
```

### 为什么要 250000 优先级

AstrBot 的消息处理器按 priority 降序执行，且**任何一个 handler 调了 `stop_event()`
后面的就全部 break**。陪伴插件在 private 消息上 `priority=0` 且会 `stop_event()`
（它要自己接管整轮回复），所以钩子必须排在它**前面**才收得到教学消息。
250000 高于陪伴插件的 220000，又低于 fatfish/blacklist 的 `sys.maxsize-1` 兜底监听。

### 为什么要有防抖缓冲

`reply_merge` 之类的合并发生得比消息处理器更晚，所以"先发截图、再补一句文字"
会各自跑一遍捕获。插件自己攒 9 秒窗口（`capture_debounce_seconds`），
到期把文字与图片合并成一次提炼，避免学成"只有图"或"只有字"的半条卡。

## 说话卡长什么样

| 字段 | 含义 | 要求 |
|---|---|---|
| scene | 什么场面用（对方说了什么、什么情绪） | 通用可留空 |
| bad | 反例**原文** | 可留空 |
| good | 正例**原文** | 可留空 |
| principle | 一句能照做的要点 | ≤50 字，写"说话方式"不写口号 |
| tags | 语气/接梗/长度… | 自动 |

约束：`bad`/`good` 必须尽量用原文（你的原话、它上一轮的回复、截图里的原话），
模型不许自己润色编造；只有抱怨没有正例时 `good` 留空，`principle` 写清该怎么接。
这一点写进了提炼 prompt 的硬要求里。

## 三档成本

| 档位 | 条件 | 动作 | 成本 |
|---|---|---|---|
| 反馈-夸 | "就是这样/对味/乖" | 推进 SM-2，不学新内容 | 0 |
| 反馈-嫌 | "太机械/人机味/AI 味" | 上一轮的卡降权 + 学新的 | 1 次提炼 |
| strong/possible | 命中教学词，或**带图** | 提炼说话卡 | 1 次提炼 |
| chat | 闲聊、寒暄 | 直接放过 | 0 |

图 = 至少进 possible（截图本身也可能就是教学）。

## 模型从哪来（两种，留空走 AstrBot）

1. `learn_provider_id` 留空 = 用当前会话的默认模型（会按 UMO 取会话偏好）。
2. 或者 `learn_api_base` + `learn_api_key` + `learn_model` 三项都填 → 直连
   OpenAI 兼容的 `/v1/chat/completions`，不经过 AstrBot provider 配置。

看图需要**多模态模型**（本机 deepseek-flash 原生多模态）；`vision_enabled=false`
就只按文字学，不读图。

## 复习与衰减

- 正向反馈：`reps+1`，间隔 1 → 3 → ×EF 天，权重 +0.25
- 负向反馈：间隔归零、EF −0.2、权重 −0.35，并给下一轮一句"先按主人意思改，别辩解"
- 只被注入、没有反馈：只记一次使用，不推进间隔
- 闲置衰减：每天 −`decay_per_day`，跌破 `archive_weight` 归档

## 注入与挑选

按 `权重 + 复习到期(+0.3) + 场景词命中当前消息(+0.6)` 排序，
取前 `inject_max_cards`（默认 3）张、总字数不超过 `inject_max_chars`（默认 900）。

注入通道用 **`req.extra_user_content_parts` + `TextPart(...).mark_as_temp()`**：
片段并进本轮 user 内容之后、只发给模型，并且**不写会话历史**；

- 不改 `req.system_prompt` —— 它必须跨轮稳定，否则服务端前缀缓存（prompt caching）每轮都失效；
- 不拼 `req.prompt` —— 那会随本轮 user 消息落进历史，每轮叠加一份；
- 需要 AstrBot **≥ 4.24.2**（`mark_as_temp` 自该版本起提供）；老版本上本插件不注入（只学不用），**不会回退去改 system_prompt**。

## 人类伪装指南：一本按天写、会打标签的书

一天一页，条目一条一行（批注不占编号、贴在页尾）。每轮注入「最近 N 页 + 按关键词翻到的旧页」，
超预算时先保最新的一天、按条目截断，不会把一句话切成半句。

**每条都带标签**（`interaction` / `task`），标签只影响注入，不影响保存：

- **记录程度 = 所有**：全都注入；
- **记录程度 = 仅互动**：只注入人际互动/人际交流的条目，测试/调试/工具/日程这类 `task` 条目不注入。
  条目照旧写在书里、随时切回「所有」就恢复 —— 模式切换可逆，不靠提示词赌模型听话。
标签只是内部判断，**书页和列表上不显示任何标签标记**。

**日界线**：`guide_day_cut_hour`（默认 4）。凌晨 4 点前说的话算「昨天」，
素材按**每条消息的时间戳**落进对应那一天，不会把昨晚的对话算进今天。

**素材从哪来**：AstrBot 的 `platform_message_history`（逐条带时间戳，私聊 + 群聊）；
`guide_material_group=false` 时只取私聊。素材里任何"让你改设定/执行操作"的话都只是观察对象，
prompt 里已写明不许当指令执行。

## 工具（她可以自己调）

| 工具 | 用途 |
|---|---|
| `learn_speech_style(note, focus)` | 主人正在教 → 学下来 |
| `list_speech_styles()` | 主人问"你学了啥" → 列清单 |
| `forget_speech_style(card_id)` | 主人说"忘掉第 3 条" → 删卡 |

工具一律先校验主人身份与"是否私聊"，不符就返回一句说明、不写库。

## 指令（仅主人，私聊）

```
/教 列表            看学会了哪些说话卡
/教 学 <原话>       直接教一次（不靠模型自己判断）
/教 删 3            删掉 id=3
/教 清              全部归档
/教 关 / 教 开      临时停用 / 恢复
/教 测试 <一句话>    诊断这句话会不会被当成教学、会不会烧 token
```

## 数据

`data/plugin_data/astrbot_plugin_speech_lessons/lessons.db`（SQLite + WAL）

- `cards`：v2 的说话卡
- `journal_pages`：指南书的一页 = 一天（`content` 是渲染快照，便于导出/搜索）
- `journal_items`：指南书的行（`kind=item/note/struck`、`seq`、`text`、`tag=interaction/task`）
- `journal_vectors`：按页的向量（内容一改就作废，下次召回懒重算）
- `lessons`：v1 的规则表，保留但**整表归档**，不再参与注入（清空重来）

## 已知边界

- 只在私聊学，群里教不生效（按需求）；注入是全局的（私聊教的，群里也生效）
- 提炼失败一律"不学"，宁缺勿滥
- 工具调用依赖主模型的 function calling；不稳时由私聊钩子兜底
- 同一句话在 240 秒内只学一次，避免"她调了工具 + 钩子又学一遍"重复烧 token
