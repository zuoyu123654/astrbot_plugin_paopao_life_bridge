# 泡泡生活桥（paopao_life_bridge）

把「我会永远陪着你」的生活和「我会牢牢记住你」的记忆接成一份，让 QQ / B站 / 桌宠 / DSH 是同一个她。

这个插件**不生产记忆**，只做翻译。

---

## 它解决什么

没有它的时候，是四个她：

- QQ 里的她，有生活（日程、心情、日记）也有记忆
- B站 里的她，只有自己的看片记录，不知道 QQ 聊过什么，也不知道你今天累不累
- 桌宠里的她，只记得自己那份会话，看不见别处
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
              ┌──────────┬───────┴───────┬──────────┐
              ▼          ▼               ▼          ▼
             QQ        B站            桌宠        DSH
                                    （记忆信箱 127.0.0.1:9889）
```

---

## 四个功能

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

### 4. 记忆信箱：桌宠和 DSH 也接进来（1.2.0 新增）

在 AstrBot 进程里开一个只监听 `127.0.0.1:9889` 的小 HTTP 服务。
桌宠网关和 DSH 都通过它读写，最终仍然落回 `service` 接口 —— 谁都不碰数据库文件。

| 方法 | 路径 | 谁用 |
|---|---|---|
| POST | `/pet/inbox` | 桌宠网关推一轮对话 |
| POST | `/memory/recall` | 桌宠 / DSH 查记忆 |
| POST | `/memory/remember` | DSH 写记忆 |
| GET | `/health` | 排障 |

可见性按来源定，写的时候定死，读的时候只过滤：

| 来源 | visibility | 谁能读 |
|---|---|---|
| 桌宠识屏 | `bot_self` | 所有端 |
| 桌宠聊天 | `private_pair` | 只有老大本人 |
| DSH 对话 | `private_pair` | 同上 |
| DSH `--self` | `bot_self` | 所有端 |

写入不是每句话都落库，而是**攒一批压成一条**：攒够 6 条（识屏 4 条）、或者聊天停下 90 秒、
或者一口气聊了 8 分钟，就调一次模型把这批压成 200–350 字的记忆。
出现「约定 / 答应 / 记住 / 喜欢 / 生日 / 怕」这类词时不等攒批，当场单独写。

这样既不丢内容，也不会把中枢刷成流水账。模型不可用时自动退回「拼原文」，宁可难看也不丢。

识屏的 `user` 字段里混着桌宠自己的提示词模板，会先被剥掉再记 —— 她不该「记得」自己收到的指令。

自带一个命令行客户端（只用标准库）：

```powershell
$py  = "<AstrBot>\venv\Scripts\python.exe"
$cli = "<AstrBot>\data\plugins\paopao_life_bridge\tools\paopao_memory.py"

& $py $cli recall "老大最近在忙什么" --limit 5
& $py $cli remember "老大说周末想去看海" --tags 约定 --importance 0.7
& $py $cli remember "我把桌宠接进中枢了" --self
& $py $cli health
```

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
| `enable_http` | `true` | 打开记忆信箱（桌宠 / DSH 靠它） |
| `http_host` | `127.0.0.1` | **不要改成 0.0.0.0** |
| `http_port` | `9889` | 记忆信箱端口 |
| `http_token` | 空 | 填了以后请求要带 `X-Paopao-Token` |
| `enable_pet_ingest` | `true` | 桌宠的对话写进中枢 |
| `enable_pet_recall` | `true` | 桌宠能读中枢 |
| `enable_external_write` | `true` | 允许 DSH 写记忆 |
| `pet_min_user_chars` | `4` | 太短不记 |
| `pet_dedupe_seconds` | `1800` | 同一句多久内不重复收 |
| `pet_summary_enabled` | `true` | 攒批总结；关掉退化成拼原文 |
| `pet_chat_summary_items` | `6` | 聊天攒够几条总结 |
| `pet_vision_summary_items` | `4` | 识屏攒够几条总结 |
| `pet_chat_summary_idle_seconds` | `90` | 聊天停多久就总结（最常用） |
| `pet_vision_summary_idle_seconds` | `120` | 识屏停多久就总结 |
| `pet_chat_summary_max_minutes` | `8` | 聊天最多攒几分钟 |
| `pet_vision_summary_max_minutes` | `15` | 识屏最多攒几分钟 |
| `pet_buffer_max_items` | `24` | 缓冲上限，超了强制总结 |
| `pet_summary_max_chars` | `320` | 一条总结最多多少字 |
| `pet_summary_provider_id` | 空 | 用哪个模型总结，空=当前默认 |
| `pet_summary_timeout` | `45` | 总结超时（秒），超了用原文兜底 |
| `pet_important_direct` | `true` | 重要的话当场单独记 |
| `pet_chat_importance` | `0.5` | 桌宠聊天记忆的重要度 |
| `pet_vision_importance` | `0.55` | 桌宠识屏记忆的重要度 |
| `pet_recall_top_k` | `5` | 桌宠 / DSH 每次读几条 |
| `pet_recall_max_chars` | `800` | 桌宠 / DSH 注入的字数上限 |
| `pet_noise_markers` | `[]` | 含这些词的桌宠输入不记（挡系统播报） |
| `pet_exclude_memory_types` | `[]` | 桌宠读记忆时排除的类型 |
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
    user_id="<主人的 B站 UID>",   # B站 UID
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
[LifeBridge] 记忆信箱已监听 http://127.0.0.1:9889
[LifeBridge] self-check memory=ok companion=ok mailbox=ok owner_qq=<你的QQ> owner_bili=<你的UID>
```

- `memory=missing` → 中枢没装 / 没启用 / 服务名不是 `service`
- `companion=missing` → 陪伴插件版本太老，或没有 `extension_api`
- `mailbox=off` → 端口被占 / `enable_http=false` / 没有 aiohttp（只影响桌宠和 DSH）

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
- **DSH 侧要她自己调**：没有自动注入，靠 `AGENT.md` 里的约定 + 命令行客户端。要做成全自动得另写一个 DSH 插件

---

## 版本

- 1.0.0 — 初版。三个功能：B站 读、QQ 补、B站 写回。
- 1.1.x — 补丁：`_plugin_data_dir` 重复定义、owner 校验、配置 BOM 兼容。
- 1.2.0 — 加记忆信箱（本地 HTTP 9889）：桌宠和 DSH 接进来。识屏记 `bot_self`、聊天记 `private_pair`，带写入节流和去重；附命令行客户端 `tools/paopao_memory.py`。
- 1.3.0 — 把「按时间节流」换成「攒批总结」：1.2.0 的节流会只留每 2 分钟的第一句，等于随机抽内容。现在攒够条数 / 聊停 90 秒 / 攒满 8 分钟就压成一条 200–350 字的记忆，重要的话当场单独写；顺手剥掉识屏里混进来的提示词模板。

兼容：AstrBot >= 4.27，陪伴插件 >= 7.0.7，记忆中枢 >= 1.10.0。
