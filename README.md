# 泡泡生活桥（paopao_life_bridge）

把「我会永远陪着你」的生活和「我会牢牢记住你」的记忆接成一份，让 QQ / B站 / DSH 是同一个她。

这个插件**不生产记忆**，只做翻译。

---

## 它解决什么

没有它的时候，是三个她：

- QQ 里的她，有生活（日程、心情、日记）也有记忆
- B站 里的她，只有自己的看片记录，不知道 QQ 聊过什么，也不知道你今天累不累
- DSH 里的她，谁也看不见

装完之后是一条链：

```
        我会永远陪着你（生活）          我会牢牢记住你（记忆）
        日程 / 心情 / 主动 / 日记        事实 / 时间线 / 可见性
                  │                            │
                  └──────────► 生活桥 ◄────────┘
                                 │
                                 ▼
                        MemoryCompanion  ← 唯一事实出口
                                 │
                   ┌─────────────┼─────────────┐
                   ▼             ▼             ▼
                 QQ            B站            DSH
```

---

## 三个功能

### 1. 给 B站 侧读中枢（`recall_for_bili`）

B站 AI Bot 每次要回评论或私信时调它，拿回一段「你记得的其他场合」。

读什么由中枢的 `visibility` 决定，不由调用方决定：

| 场合 | 能读到的 |
|---|---|
| 评论区 | `bot_self`（她自己的经历）、`shareable`（可以到处说的） |
| B站 私信（对方不是老大） | 同上 |
| B站 私信（对方是 `owner_bili`） | 再加上 `private_pair`——他自己的私聊记忆 |

`private_pair` 永远不进评论区。这是硬规则，不靠模型自觉。

### 2. QQ 私聊补充 B站 的事（Provider `read`）

向「我会永远陪着你」注册官方外部记忆 Provider（`companion.external-memory.v1`）。
QQ 私聊召回时，它把**中枢里 B站 来源**的条目补进去，避免她和内置记忆重复。

只返回 `source_plugin` 以 `bilibili` 开头的条目。

### 3. B站 的事写回中枢（`remember_bili`）

看片、发动态、回评论之后，B站 AI Bot 调它写一条中枢记忆：

- `visibility="bot_self"` → 她自己的经历，所有端可读
- BV 做幂等键，同一个视频反复写只留一条
- `reality` 决定这条经历有多「实」：
  - `bot_action`（默认）——真做过：看了视频、发了动态
  - `media_index`——只拿到文字资料（标题/简介/字幕），没看画面
  - `observed`——只看到别人的东西

写不准她会吹牛说「我看了」，被追问细节就露馅。

---

## 为什么 `enable_journal_ingest` 默认关闭

陪伴插件自己的旧桥（`record_persona_life` / `record_proactive_message`）已经在把日记、梦境、主动消息写进中枢了，`source_plugin=astrbot_plugin_private_companion`。

如果生活桥再注册一个 `write`，官方 Provider 的优先级会让它**顶掉**旧桥，按这边朴素的 `record_external_event` 格式重写一遍——结果是丢掉 `persona_life` 的专门分类和权重。

所以默认 `false`。只有确认旧桥不可用（中枢里长期没有 `bot_self` + `source_plugin=astrbot_plugin_private_companion` 的记录）时才打开。

查一下：

```sql
SELECT visibility, source_plugin, count(*) FROM memories
WHERE visibility = 'bot_self' GROUP BY visibility, source_plugin;
```

---

## 安装

把整个目录放进 `AstrBot/data/plugins/paopao_life_bridge/`，重启 AstrBot。

依赖（必须已装并启用）：

| 插件 | 最低版本 | 用来干什么 |
|---|---|---|
| `astrbot_plugin_private_companion` | 7.0.7 | 提供 `extension_api` 和 `register_memory_provider` |
| `astrbot_plugin_memory_companion` | 1.10.x | 提供 `service.record_external_event` / `bridge_search` |

装了但没启用，桥会降级成空操作，只打一条自检日志。

---

## 配置

配置文件：`AstrBot/data/config/paopao_life_bridge_config.json`

| 键 | 默认 | 说明 |
|---|---|---|
| `enable` | `true` | 总开关。关掉后所有跨端读写静默跳过 |
| `owner_qq` | — | 老大的 QQ 号。**身份归一的锚点** |
| `owner_bili` | — | 老大的 B站 UID |
| `enable_bili_recall` | `true` | 允许 B站 侧读中枢 |
| `enable_journal_ingest` | `false` | 见上一节。一般不打开 |
| `enable_qq_supplement` | `true` | 允许给 QQ 私聊补 B站 记忆 |
| `bili_recall_top_k` | `5` | B站 侧每次最多读几条 |
| `bili_recall_max_chars` | `700` | B站 侧注入的字数上限（整条取舍，不截断） |
| `qq_recall_top_k` | `3` | QQ 私聊补充几条 |
| `debug` | `false` | 每次读写打一行摘要 |

`owner_qq` / `owner_bili` 填错不会报错，只会表现为「B站 私信里的她把你当陌生人」。这是最难查的一种。

---

## 对外接口

给 `astrbot_plugin_bilibili_ai_bot` 用的两个方法，都在插件实例上：

```python
bridge = context.get_registered_star("paopao_life_bridge").star_cls

# 读：返回可以直接拼进提示词的字符串，读不到就是 ""
block = await bridge.recall_for_bili(
    query,                      # 要对齐的语义，一般是当前这条消息
    channel="comment",          # "comment" / "private" / "live"
    user_id="1252860980",       # B站 UID
    oid="123456",               # 视频/动态 oid，可空
    thread_id="bili:...",       # 评论线，可空
)

# 写：返回 memory_id，失败返回 ""
mid = await bridge.remember_bili(
    "看了《悬链线》讲桥为什么不塌",
    kind="video",               # watch / video / dynamic / comment / bangumi
    visibility="bot_self",
    bvid="BV1xx",
    tags=["B站", "video"],
)

# 健康检查
bridge.health()   # {"enabled":..., "memory_service":..., "companion_provider":..., "writes":..., "last_error":...}
```

两个方法都不抛异常。桥挂了只会让她忘事，不会让 B站 回复变慢或报错。

---

## 自检

重启后日志里应该有：

```
[LifeBridge] self-check memory=ok companion=ok owner_qq=1792559819 owner_bili=1252860980
```

- `memory=missing` → 中枢没装 / 没启用 / 服务名不是 `service`
- `companion=missing` → 陪伴插件版本太老，或没有 `extension_api`

---

## 排障

| 现象 | 先查 |
|---|---|
| 日志完全没有 `[LifeBridge]` | 目录层级不对（AstrBot 只扫描 `data/plugins/` 一层）、`metadata.yaml` 缺失、语法错误 |
| `companion=missing` | 陪伴插件 < 7.0.7，或者 `docs/external-memory-provider.md` 不存在 |
| B站 回复里没有【你记得的其他场合】 | 中枢里没有 `bot_self` / `shareable` 记忆，或 B站 AI Bot 没调 `recall_for_bili` |
| 评论区说出了 QQ 的事 | 查那条记忆的 `visibility`，一定被写成了 `shareable` |
| 一次刷片两条记忆 | `bili_bridge` 的 `sync_enabled` 还开着，或 `enable_journal_ingest` 被打开了 |

---

## 已知限制

- **不知道你在另一边正在说什么**：没有实时会话共享，两边并发各说各的
- **生活只到「天」粒度**：陪伴插件推的是已完成的日记/梦境，不是实时状态
- **B站 评论区永远是半个她**：公开场合只给 `bot_self` + `shareable`，这是设计
- **QQ 群接不上 B站**：官方 Provider 的 `read` 只在已授权私聊调用

---

## 版本

- 1.0.0 — 初版。三个功能：B站 读、QQ 补、B站 写回。

兼容：AstrBot >= 4.27，陪伴插件 >= 7.0.7，记忆中枢 >= 1.10.0。
