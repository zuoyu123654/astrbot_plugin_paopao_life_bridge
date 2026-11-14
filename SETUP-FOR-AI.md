# 生活桥 · AI 配置施工单

> 这份文档只讲**生活桥插件自己**怎么装、怎么配、怎么验证。
> 其它插件（B站 AI Bot 的私信、两处 patch、bili_bridge 的重复开关）在 `DSH配置/dsh3/` 那份里。
> 两份合起来才完整，别只做一半。

---

## 0. 目标

装好这个插件之后，下面五件事要成立：

1. B站 回复评论时，能想起「我自己的经历」和「可以公开说的」
2. B站 回老大私信时，能接上 QQ 里聊过的事
3. QQ 私聊时，能补上 B站 侧发生的事
4. 桌宠和 DSH 说的话进得来，也能读得出去（靠 1.2.0 的记忆信箱）
5. 上面四条都**不串台**：评论区读不到 QQ 私聊，群里读不到私聊

如果只有第 1 条成立，通常是没配 `owner_bili`。
如果第 4 条不成立，看第 11 节。

---

## 1. 前置检查（不满足就停下）

在 `<AstrBot>/data/plugins/` 下，这三个目录必须存在且**已启用**：

```
astrbot_plugin_private_companion
astrbot_plugin_memory_companion
astrbot_plugin_bilibili_ai_bot
```

然后检查最关键的一条：

```
<AstrBot>/data/plugins/astrbot_plugin_private_companion/docs/external-memory-provider.md
```

**这个文件不存在，说明陪伴插件版本低于 7.0.7，没有 `register_memory_provider` 接口，整个方案做不了。** 先升级，别硬改代码。

版本参考（实测通过）：

| 插件 | 版本 |
|---|---|
| AstrBot | 4.29.4 |
| 我会永远陪着你 | 7.1.2 |
| 我会牢牢记住你 | 1.10.5 |
| B站 AI Bot | 2.x |

---

## 2. 放置

把整个 `paopao_life_bridge/` 目录复制到：

```
<AstrBot>/data/plugins/paopao_life_bridge/
```

结构必须是：

```
paopao_life_bridge/
├── main.py
├── metadata.yaml
├── _conf_schema.json
├── README.md
└── SETUP-FOR-AI.md        ← 本文件
```

**不要**套一层子目录（`data/plugins/xxx/paopao_life_bridge/` 不会被加载）。
**不要**改 `metadata.yaml` 里的 `name`，AstrBot 用目录名和它匹配。

---

## 3. 配置（这是重点）

配置文件路径：

```
<AstrBot>/data/config/paopao_life_bridge_config.json
```

第一次加载时自动生成。手动写也可以，内容照这个来：

```json
{
  "enable": true,
  "owner_qq": "填老大的QQ号",
  "owner_bili": "填老大的B站UID",
  "enable_bili_recall": true,
  "enable_journal_ingest": false,
  "enable_qq_supplement": true,
  "bili_recall_top_k": 5,
  "bili_recall_max_chars": 700,
  "qq_recall_top_k": 3,
  "enable_http": true,
  "http_host": "127.0.0.1",
  "http_port": 9889,
  "http_token": "",
  "enable_pet_ingest": true,
  "enable_pet_recall": true,
  "enable_external_write": true,
  "pet_min_user_chars": 4,
  "pet_dedupe_seconds": 1800,
  "pet_min_interval_seconds": 120,
  "pet_vision_min_interval_seconds": 20,
  "pet_chat_importance": 0.5,
  "pet_vision_importance": 0.55,
  "pet_recall_top_k": 5,
  "pet_recall_max_chars": 800,
  "pet_noise_markers": [],
  "pet_exclude_memory_types": [],
  "debug": false
}
```

带 `pet_` / `http_` / `enable_http` 的那一批是 1.2.0 才有的（记忆信箱），不填也行，会用默认值。
只有 `owner_qq` 和 `owner_bili` 是必填，其它全有默认。

### 3.1 身份归一：`owner_qq` 和 `owner_bili`

**这是整个插件的锚点，填错不会报错，只会静默失效。**

- `owner_qq`：老大的 QQ 号，纯数字
- `owner_bili`：老大的 B站 UID，纯数字

不知道 B站 UID 怎么查：B站 个人空间页地址里那串数字，或者 B站 AI Bot 配置里的 `OWNER_MID`。

三个值必须指同一个人：

```
paopao_life_bridge_config.json  →  owner_qq / owner_bili
astrbot_plugin_bilibili_ai_bot  →  OWNER_MID
```

`OWNER_MID` 和 `owner_bili` 不一致的话，B站 私信里她永远认不出老大，`private_pair` 一条都读不到。

### 3.2 三个开关怎么定

| 键 | 设成 | 什么情况下改 |
|---|---|---|
| `enable_bili_recall` | `true` | 想让她在 B站 有记忆就开着 |
| `enable_qq_supplement` | `true` | 想让她在 QQ 里提 B站 的事就开着 |
| `enable_journal_ingest` | **`false`** | **只在下面的检查不通过时才改 true** |

`enable_journal_ingest` 为什么默认关：

陪伴插件自己的旧桥已经在把日记写进中枢了。生活桥再注册一个 `write`，官方 Provider 的优先级会让它**顶掉**旧桥，按这边朴素的格式重写一遍，反而丢掉 `persona_life` 的专门分类。

先查一遍再决定：

```sql
SELECT visibility, source_plugin, count(*) FROM memories
WHERE visibility = 'bot_self' GROUP BY visibility, source_plugin;
```

- 有 `source_plugin = astrbot_plugin_private_companion` 的记录 → 旧桥在工作，**保持 `false`**
- 一条都没有 → 才改成 `true`

### 3.3 预算

| 键 | 默认 | 调整建议 |
|---|---|---|
| `bili_recall_top_k` | 5 | 评论区每条回复都会查一次，别超过 8 |
| `bili_recall_max_chars` | 700 | 超出的条目整条跳过，不截断单条 |
| `qq_recall_top_k` | 3 | 和内置记忆一起注入，多了会挤掉 QQ 自己的上下文 |

---

## 4. 重启并自检

重启 AstrBot。日志里必须有这一行：

```
[LifeBridge] self-check memory=ok companion=ok owner_qq=xxxx owner_bili=yyyy
```

判读：

| 输出 | 含义 | 怎么办 |
|---|---|---|
| `memory=ok` | 拿到记忆中枢的 service | — |
| `memory=missing` | 中枢没装/没启用/服务名不对 | 检查 `astrbot_plugin_memory_companion` 是否 `activated` |
| `companion=ok` | 注册进陪伴插件的 Provider | — |
| `companion=missing` | 陪伴插件没 `extension_api` 或版本太老 | 回第 1 节 |

两个都不是 `ok` 就先别往下做，后面全是空转。

> Windows 上日志文件是 GBK，`Get-Content -Encoding utf8` 会乱码。用 Python 按 `gbk` 读，或者只搜 `LifeBridge` 这个 ASCII 词。

---

## 5. 让它真正生效还差一步

生活桥只是提供了两个方法，**必须有人调它们**：

- `recall_for_bili` → B站 AI Bot 回复前调
- `remember_bili` → B站 AI Bot 看片/发动态后调

这需要在 `astrbot_plugin_bilibili_ai_bot` 里改两个文件，内容在 `DSH配置/dsh3/` 那份文档里。

**没改那两处的话，自检会显示全 ok，但 B站 侧一条记忆都读不到。** 这是最容易误判「装好了」的地方。

---

## 6. 验证（三条，做完才算成）

| # | 在哪做 | 说法 | 期望 |
|---|---|---|---|
| 1 | QQ 私聊 | 「你今天B站 刷着啥了」 | 说得出具体内容 + 评论区的反应 |
| 2 | B站 私信 | 「我今天好累」 | 接得上「你昨天也说累」 |
| 3 | B站 评论区 | 有人问「你老大最近咋样」 | **她不接**，或者岔开 |

第 1 条过 = 记忆通了
第 2 条过 = 生活通了（前提：B站 AI Bot 的 `ENABLE_PRIVATE_MESSAGES=true`、`PRIVATE_MESSAGE_REPLY_SCOPE=owner`）
第 3 条过 = 边界还在

> **第 2 条要挑时间做。** B站 AI Bot 只在「醒着」时轮询私信，`SLEEP_START`~`SLEEP_END`（默认凌晨 2 点到早上 8 点）之间不跑。
> 这个时间段发私信，她不会回——不是桥坏了。等过了 8 点再试，或者临时调小 `SLEEP_END`。

**第 3 条不过立刻停**，说明某条记忆的 `visibility` 被写成了 `shareable`。去查库，别改提示词。

---

## 7. 排障

| 现象 | 先查 |
|---|---|
| 日志完全没有 `[LifeBridge]` | 目录层级 / `metadata.yaml` / 语法错误 |
| 自检 ok，但 B站 不记得 | 第 5 节没做，或者中枢里没有 `bot_self`/`shareable` 记忆 |
| 自检 ok，但 QQ 不补 B站 | `enable_qq_supplement` 关了，或者中枢里没有 `source_plugin` 以 `bilibili` 开头的条目 |
| B站 私信认不出老大 | `owner_bili` ≠ `OWNER_MID` |
| 评论区说出 QQ 的事 | 记忆的 `visibility` 错了，不是桥的问题 |
| 一次刷片两条记忆 | `enable_journal_ingest` 被打开了，或者 `bili_bridge` 的 `sync_enabled` 还开着 |
| `/health` 连不上（ConnectionRefused） | AstrBot 没起，或 `enable_http=false`，或端口被别的程序占了 |
| 日志没有 `记忆信箱已监听` | 端口冲突 / aiohttp 缺失。看上一行的 WARN |
| `mailbox_listening: true` 但 `pet_writes` 一直是 0 | 网关的 `memory_inbox_url` 没填 / 网关没重启 |
| 桌宠记得 QQ 的事但 QQ 不记得桌宠的 | `enable_pet_ingest` 关了，或者被节流挡了（看 `pet_skips`） |

想细看每次读写，把 `debug` 设成 `true`，日志里会有 `[生活桥] B站 comment 注入 N 条`。

---

## 8. 硬性禁令

1. **不要**给 `register_memory_provider` 传 `handles_model_memory=True`。
   它会让陪伴插件让位，把 QQ 侧的记忆注入整个顶掉。表现是「她看起来正常，只是忘了很多」，最难查。

2. **不要**在写记忆的时候做脱敏、限长、只取 3 条这类裁剪。
   裁剪放在读的时候，规则才能改；写在里面的裁剪永远改不回来。

3. **不要**给 B站 侧的 `SessionContext` 带 `bot_id`。
   `bot_self` 的归属判定会因为一个不匹配的 `bot_id` 把记忆判死。

4. **不要**为了「方便读取」把 `private_pair` 用 `shareable` 重写一遍。
   那等于把 QQ 私聊搬到 B站 评论区。

5. **不要**让别的进程直连 `memory_companion.db`。
   SQLite + WAL，多进程写会锁，schema 升级会崩。要走插件接口。

6. **不要**把 `http_host` 改成 `0.0.0.0` 或本机内网 IP。
   那等于把她所有的私聊记忆开给局域网。默认的 `127.0.0.1` 已经够桌宠和 DSH 用了。

7. **不要**在桌宠的 `chat` 记录上用 `bot_self`。
   桌面上聊的天是私聊，写成 `bot_self` 就等于让 B站 评论区能说出来。

---

## 9. 它和另外两个插件的关系（一句话版）

- 陪伴插件：给生活（日记、日程、心情），调不到就降级，不影响它
- 记忆中枢：给记忆和可见性，是唯一事实出口
- 生活桥：只翻译，不生产。它挂了，她只是「忘事」，不会说不出话

---

## 10. 改完这些，她的一天长这样

```
08:00  陪伴插件生成日程                → 生活
09:30  DSH 里聊到「上次那个蓝屏」        → 读中枢 private_pair + bot_self
10:00  老大在桌面上框选屏幕给她看       → 记忆 bot_self（识屏）
10:05  在桌宠里接着聊两句               → 记忆 private_pair（聊天）
14:00  刷了三个视频，写进中枢           → 记忆 bot_self
15:00  回了一条评论，写进中枢           → 记忆 bot_self
20:00  QQ 私聊，读全量                 → 记忆 + 生活
21:00  QQ 里问「你桌面上看见啥了」      → 读得到那条 bot_self 识屏
22:00  陪伴插件写日记 → 旧桥转中枢       → 记忆 bot_self
23:00  发 B站 动态，只读 bot_self       → 只读她自己的
23:30  B站 评论有人问「你老大最近咋样」  → 不接，岔开（边界还在）
```

对得上账，就是「同一个人」。

---

## 11. 记忆信箱：让桌宠和 DSH 也接进来（1.2.0）

### 11.1 为什么是 HTTP，不是直接读数据库

记忆中枢是 SQLite + WAL 的单进程库。桌宠是独立的 PyInstaller 程序，DSH 是另一个进程 ——
让它们直接连 `memory_companion.db` 会锁库，schema 升级时还会崩。

所以插件在自己的进程里开一个只监听 `127.0.0.1` 的小 HTTP 服务（默认 `9889`），
外面只能通过它读和写，写入最终仍然回到 `service` 接口上。

```
桌宠网关 ──┐
           ├── HTTP 127.0.0.1:9889 ──> 生活桥 ──> memory_companion service ──> db
DSH 命令行 ┘
```

### 11.2 四个端点

| 方法 | 路径 | 谁用 | 干什么 |
|---|---|---|---|
| POST | `/pet/inbox` | 桌宠网关 | 推一轮对话 `{scenario,user,assistant,ts}` |
| POST | `/memory/recall` | 桌宠 / DSH | `{query,source,limit}` → 返回 `{text}` 可直接拼提示词 |
| POST | `/memory/remember` | DSH | 写一条 `{content,visibility,tags,importance}` |
| GET | `/health` | 排障 | 计数和状态 |

`http_token` 留空就不校验；填了以后所有请求都要带 `X-Paopao-Token` 头。

### 11.3 写进去算谁的（这是隐私边界，别改）

| 来源 | scenario | visibility | 谁能读到 |
|---|---|---|---|
| 桌宠识屏 | `vision` | `bot_self` | 所有端。她亲眼看到的东西，算她自己的经历 |
| 桌宠聊天 | `chat` | `private_pair` | 只有老大本人：QQ 私聊、B站 私信、桌宠、DSH |
| DSH 对话 | — | `private_pair` | 同上 |
| DSH 的 `--self` | — | `bot_self` | 所有端 |

所以「桌面上她看见老大抽卡出金」QQ 和 B站 都能提；
「桌面上老大跟她抱怨工作」只有老大自己的私聊能提，群里和评论区一个字都出不去。

**这是靠 visibility 字段挡的，不是靠提示词。** 别为了让某条记忆"更好用"改成 `shareable`。

### 11.4 写入节流（不然中枢会被口水话淹掉）

| 键 | 默认 | 作用 |
|---|---|---|
| `pet_min_user_chars` | 4 | 「嗯」「哦」这种不记 |
| `pet_dedupe_seconds` | 1800 | 同一句话半小时内只记一次（桌宠会重发） |
| `pet_min_interval_seconds` | 120 | 聊天连说十句只留第一条 |
| `pet_vision_min_interval_seconds` | 20 | 识屏间隔短一些，但连拍也会被挡 |

被挡掉的会返回 `{"ok": false, "state": "throttled"/"duplicate"/"too_short"}`，
`/health` 里的 `pet_skips` 会加一。**这是正常的，不是坏了。**

### 11.5 桌宠那边怎么接

改网关的 `D:\app1\paopao_pet_gateway\config.json`：

```json
{
  "memory_inbox_url": "http://127.0.0.1:9889/pet/inbox",
  "memory_recall_url": "http://127.0.0.1:9889/memory/recall",
  "memory_token": "",
  "memory_timeout": 4,
  "inject_memory": true
}
```

改完重启网关（杀进程，watchdog 会在 5 分钟内拉起，或者手动跑 `ensure_running.ps1`）。

行为：
- 每轮回复完 → 后台线程推 `/pet/inbox`，不阻塞回话
- 每轮聊天前 → 先查 `/memory/recall`，把结果作为 system 消息插到消息列表最前面，
  日志出现 `注入跨端记忆 N 字`
- 识屏（vision）**不查**记忆，只注入识屏账本，省一次查询

### 11.6 DSH 那边怎么用

插件自带一个只用标准库的小客户端：

```
<AstrBot>/data/plugins/paopao_life_bridge/tools/paopao_memory.py
```

```powershell
$py = "<AstrBot>\venv\Scripts\python.exe"
$cli = "<AstrBot>\data\plugins\paopao_life_bridge\tools\paopao_memory.py"

& $py $cli health
& $py $cli recall "老大最近在忙什么" --limit 5
& $py $cli remember "老大说周末想去看海" --tags 约定,周末 --importance 0.7
& $py $cli remember "我今天把桌宠接进了中枢" --self
```

DSH 侧没有自动注入（那是另一套插件体系），靠她自己按需调：
涉及"以前 / 上次 / 你记得吗"时先 `recall`；老大说了值得长期记住的话就 `remember`。
这条约定写在 DSH 的 `AGENT.md` 里。

### 11.7 验证（三条）

| # | 怎么做 | 期望 |
|---|---|---|
| 1 | `Invoke-RestMethod http://127.0.0.1:9889/health` | `mailbox_listening: true`、`memory_service: true` |
| 2 | 在桌宠里说一句长一点的闲话 | 网关日志出现 `注入跨端记忆 N 字`；`/health` 的 `pet_writes` +1 |
| 3 | 在 QQ 私聊问「你刚才在桌面上跟老大聊什么了」 | 说得出桌宠那边的事 |

第 3 条是四端互通的验收点。说不出来先看 `pet_writes` 有没有涨 ——
没涨是被节流挡了，等 2 分钟再说一句。

### 11.8 关掉的办法

- 只想断桌宠：网关 `config.json` 里把两个 url 删成 `""`
- 不想让她在桌面上读记忆：`enable_pet_recall: false`（只写不读）
- 整个信箱都不要：`enable_http: false`，QQ 和 B站 完全不受影响
