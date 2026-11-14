"""泡泡生活桥 —— 把「我会永远陪着你」的生活和「我会牢牢记住你」的记忆接成一份。

设计要点（改动前先读这一段）：

1. 桥不生产记忆。它只做两件翻译：
   - 陪伴插件的日记/梦境/主动消息  →  记忆中枢的 bot_self 记忆
   - 记忆中枢里她自己的经历        →  别的端回复时要看的"我记得的其他场合"
2. 写的时候决定可见性，读的时候只做过滤。**不要在写的时候脱敏**，否则别的端永远
   拿不回细节；也不要在读的时候放宽，否则评论区会说漏 QQ 的私事。
3. 所有跨端调用都包在 try/except 里。桥挂了只会让她"忘事"，不能让她不回话。
4. 幂等键用源记录的 memory_id，重放不会写两条。

从 1.2.0 起多了一层「记忆信箱」（只在 127.0.0.1 上监听）：

    POST /pet/inbox        桌宠网关推来一轮对话（{scenario,user,assistant,ts}）
    POST /memory/recall    任何本地进程按私聊语义召回一段可直接拼进提示词的文字
    POST /memory/remember  任何本地进程写一条记忆
    GET  /health           健康检查

为什么要走 HTTP 而不是让桌宠直连数据库：
记忆中枢是 SQLite + WAL，多进程写会锁库，schema 升级时会崩。桌宠是独立的
PyInstaller 程序，绝对不能碰那个 .db。所有写入必须回到 AstrBot 进程里，
经由 service 接口完成 —— 这就是这层信箱存在的唯一理由。

配对的两个插件：
  - astrbot_plugin_private_companion（我会永远陪着你）→ 生活、日程、心情、日记
  - astrbot_plugin_memory_companion（我会牢牢记住你）→ 记忆、时间线、可见性
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

try:  # aiohttp 是 AstrBot 的依赖，正常情况下一定在
    from aiohttp import web
except Exception:  # pragma: no cover
    web = None

PLUGIN_NAME = "paopao_life_bridge"
PLUGIN_VERSION = "1.2.0"

COMPANION_PLUGIN = "astrbot_plugin_private_companion"
MEMORY_PLUGIN = "astrbot_plugin_memory_companion"
PROVIDER_API_VERSION = "companion.external-memory.v1"
PROVIDER_ID = "paopao.life_bridge"

#: 这些来源写进中枢后，才算"B站 侧的事"，QQ 补充记忆时只挑这些。
BILI_SOURCE_PREFIXES = ("bilibili", "paopao_life_bridge.bili")

#: 陪伴插件推来的 kind 映射到中枢的 memory_type。
LIFE_KIND_TO_TYPE = {
    "persona_life": "journal",
    "proactive_message": "proactive",
}

#: 桌宠网关推来的 scenario 决定这条记忆算谁的。
PET_SCENARIO_KIND = {
    "vision": "pet_vision",   # 她亲眼看到的屏幕 → bot_self，所有端可读
    "chat": "pet_chat",       # 桌面上聊的天      → private_pair，只对老大本人
}

MAX_CONTENT = 1200
_SPACES = re.compile(r"\s+")


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


def _squash(value: Any, limit: int = 200) -> str:
    """把换行和连续空格压成单个空格，记忆里不该出现大段换行。"""
    return _SPACES.sub(" ", str(value or "")).strip()[:limit]


@register(
    PLUGIN_NAME,
    "paopao",
    "生活桥：把陪伴插件的生活和记忆中枢接成一份，让 QQ / B站 / 桌宠 / DSH 是同一个她。",
    PLUGIN_VERSION,
    "https://local.invalid/paopao_life_bridge",
)
class PaopaoLifeBridge(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = config or {}
        self._registration: Any = None
        self._companion_api: Any = None
        self.last_write_at: float = 0.0
        self.last_read_at: float = 0.0
        self.write_count: int = 0
        self.read_count: int = 0
        self.last_error: str = ""

        # 记忆信箱
        self._http_runner: Any = None
        self._http_site: Any = None
        self.pet_write_count: int = 0
        self.dsh_write_count: int = 0
        self.pet_skip_count: int = 0
        self._pet_seen: dict[str, float] = {}
        self._pet_last_write: dict[str, float] = {"chat": 0.0, "vision": 0.0}

    # ══════════════════════════════════════════
    #  配置
    # ══════════════════════════════════════════

    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on", "启用"}

    def _int(self, key: str, default: int, minimum: int = 0, maximum: int | None = 50) -> int:
        try:
            value = int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default
        value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def _float(self, key: str, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
        try:
            value = float(self._cfg(key, default))
        except (TypeError, ValueError):
            return default
        return max(minimum, min(maximum, value))

    @property
    def enabled(self) -> bool:
        return self._bool("enable", True)

    @property
    def owner_qq(self) -> str:
        return _clean(self._cfg("owner_qq", ""), 40)

    @property
    def owner_bili(self) -> str:
        return _clean(self._cfg("owner_bili", ""), 40)

    @property
    def debug(self) -> bool:
        return self._bool("debug", False)

    # ══════════════════════════════════════════
    #  插件发现
    # ══════════════════════════════════════════

    def _find_star(self, *names: str):
        wanted = {n for n in names if n}
        if not wanted:
            return None
        try:
            stars = self.context.get_all_stars()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"get_all_stars: {exc}"
            return None
        for star in stars or []:
            if not getattr(star, "activated", False):
                continue
            ids = {
                str(getattr(star, "root_dir_name", "") or ""),
                str(getattr(star, "name", "") or ""),
            }
            if ids & wanted:
                return star
        return None

    def _memory_service(self):
        """拿记忆中枢的 service；它一重载就要重新拿，绝不缓存。"""
        star = self._find_star(MEMORY_PLUGIN)
        if star is None:
            return None
        service = getattr(getattr(star, "star_cls", None), "service", None)
        if service is None or not hasattr(service, "record_external_event"):
            return None
        return service

    def _find_companion_api(self):
        """拿陪伴插件的公开扩展门面；重载后要重新发现。"""
        star = self._find_star(COMPANION_PLUGIN)
        if star is None:
            return None
        api = getattr(getattr(star, "star_cls", None), "extension_api", None)
        if api is None:
            return None
        if getattr(api, "memory_provider_api_version", None) != PROVIDER_API_VERSION:
            return None
        return api

    # ══════════════════════════════════════════
    #  生命周期
    # ══════════════════════════════════════════

    async def initialize(self):
        if not self.enabled:
            logger.info("[生活桥] 未启用，跳过注册")
            return
        ok = await self._register_provider()
        await self._start_http()
        try:
            has_memory = self._memory_service() is not None
        except Exception:
            has_memory = False
        logger.info(
            "[LifeBridge] self-check memory=%s companion=%s mailbox=%s owner_qq=%s owner_bili=%s",
            "ok" if has_memory else "missing",
            "ok" if ok else "missing",
            "ok" if self._http_site is not None else "off",
            self.owner_qq or "?",
            self.owner_bili or "?",
        )
        missing = [
            name for name, value in (("owner_qq", self.owner_qq), ("owner_bili", self.owner_bili))
            if not value
        ]
        if missing:
            logger.warning(
                "[LifeBridge] %s 还没填 —— 这是必填项。留空时 B站 私信里认不出主人，"
                "private_pair 的记忆一条都读不到",
                " / ".join(missing),
            )

    async def terminate(self):
        await self._stop_http()
        api = self._companion_api
        registration = self._registration
        self._registration = None
        self._companion_api = None
        if api is not None and registration is not None:
            try:
                api.unregister_memory_provider(registration)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[生活桥] 注销失败：{exc}")

    async def _register_provider(self) -> bool:
        api = self._find_companion_api()
        if api is None:
            logger.warning(
                "[生活桥] 没找到「我会永远陪着你」的 extension_api（或版本不匹配），"
                "生活记录暂时不会进中枢"
            )
            return False
        try:
            self._registration = api.register_memory_provider(
                PROVIDER_ID,
                plugin=self,
                display_name="泡泡生活桥",
                read=self._provider_read if self._bool("enable_qq_supplement", True) else None,
                write=self._provider_write if self._bool("enable_journal_ingest", False) else None,
                # 绝不设 True：那会让陪伴插件让位，把 QQ 侧的记忆注入整个顶掉。
                handles_model_memory=False,
                is_active=lambda: self.enabled,
                priority=0,
                timeout_seconds=5.0,
            )
            self._companion_api = api
            logger.info("[生活桥] 已接入陪伴插件：日记/梦境/主动消息会写进记忆中枢")
            return True
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"register_provider: {exc}"
            logger.warning(f"[生活桥] 注册外部记忆 Provider 失败：{exc}")
            return False

    # ══════════════════════════════════════════
    #  Provider 回调（陪伴插件 → 桥）
    # ══════════════════════════════════════════

    async def _provider_write(self, record: dict[str, Any]) -> str | None:
        """收下日记 / 梦境 / 主动消息，写成中枢里她自己的经历（bot_self）。

        默认关闭：陪伴插件自己的旧桥已经在做同样的事，多注册一个 writer 会顶掉它、
        并按这里的朴素格式重写一遍，反而把 persona_life 的专门分类弄丢。
        只有确认旧桥不可用（中枢里长期没有 source_plugin=astrbot_plugin_private_companion
        的 bot_self 记忆）时才打开 enable_journal_ingest。
        """
        if not self.enabled or not self._bool("enable_journal_ingest", False):
            return None
        service = self._memory_service()
        if service is None:
            return None
        try:
            source = _clean(record.get("source_plugin"), 80) or COMPANION_PLUGIN
            kind = _clean(record.get("kind"), 40) or "persona_life"
            memory_id = _clean(record.get("memory_id"), 120)
            content = _clean(record.get("content"), MAX_CONTENT)
            if not content:
                return None
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            label = _clean(metadata.get("subtype") or metadata.get("journal_kind") or kind, 40)
            body = f"【{label}】{content}"
            occurred_at = _clean(record.get("occurred_at"), 80) or datetime.now().isoformat()
            tags = ["生活", label]
            for extra in record.get("tags") or []:
                text = _clean(extra, 30)
                if text and text not in tags:
                    tags.append(text)
            await service.record_external_event(
                content=body,
                memory_type=LIFE_KIND_TO_TYPE.get(kind, "journal"),
                # bot_self：她自己的生活，所有场合都能读到，但不挂在任何用户身上。
                visibility="bot_self",
                scope="private",
                platform="companion",
                session_id="companion:life",
                reality_level="bot_action",
                lifecycle="stable_memory",
                confidence=0.9,
                importance=0.6,
                review_status="auto",
                tags=tags[:8],
                metadata={
                    "owner_bot_id": "",
                    "source_kind": kind,
                    "source_label": label,
                    "life_bridge": PROVIDER_ID,
                },
                source_plugin=PLUGIN_NAME,
                occurred_at=occurred_at,
                memory_id=memory_id or f"life:{occurred_at}:{content[:24]}",
            )
            self.write_count += 1
            self.last_write_at = time.time()
            if self.debug:
                logger.info(f"[生活桥] 生活记忆已入中枢：{label} / {content[:40]}")
            return memory_id or "life"
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"provider_write: {exc}"
            logger.warning(f"[生活桥] 写生活记忆失败：{exc}")
            return None

    async def _provider_read(self, request: dict[str, Any]):
        """QQ 私聊召回时，补上 B站 侧发生的事。只返回 B站 来源，避免自我重复。"""
        if not self.enabled or not self._bool("enable_qq_supplement", True):
            return {"items": []}
        service = self._memory_service()
        if service is None:
            return {"items": []}
        try:
            query = _clean(request.get("query"), 600)
            if not query:
                return {"items": []}
            limit = min(int(request.get("limit") or 3), self._int("qq_recall_top_k", 3, 1, 10))
            max_chars = int(request.get("max_chars") or 600)
            items = await service.bridge_search(
                query,
                session_context=self._bili_context(scope="private", user_id=self.owner_qq, target="qq"),
                top_k=max(limit * 3, 8),
            )
            picked = []
            used = 0
            for item in items or []:
                content = _clean(item.get("content"), 400)
                if not content:
                    continue
                if not self._is_bili_item(item):
                    continue
                if used + len(content) > max_chars and picked:
                    break
                used += len(content)
                picked.append({
                    "id": _clean(item.get("id"), 120),
                    "content": content,
                    "occurred_at": _clean(item.get("occurred_at"), 40),
                    "source": "bilibili",
                })
                if len(picked) >= limit:
                    break
            if picked:
                self.read_count += 1
                self.last_read_at = time.time()
                if self.debug:
                    logger.info(f"[生活桥] 给 QQ 私聊补了 {len(picked)} 条 B站 记忆")
            return {"items": picked}
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"provider_read: {exc}"
            logger.warning(f"[生活桥] 读 B站 记忆失败：{exc}")
            return {"items": []}

    # ══════════════════════════════════════════
    #  对外接口（给 B站 AI Bot 用）
    # ══════════════════════════════════════════

    def map_bili_user(self, bili_uid: str) -> str:
        """B站 UID → 归一身份。老大本人映射到 QQ 号，其他人原样返回。"""
        uid = _clean(bili_uid, 40)
        owner_bili = self.owner_bili
        if uid and owner_bili and uid == owner_bili:
            return self.owner_qq or uid
        return uid

    def _bili_context(self, *, scope: str, user_id: str, target: str, oid: str = "", thread_id: str = "") -> dict[str, Any]:
        group_id = f"bili:{oid}" if oid else ""
        return {
            "session_id": f"bili:{target}:{oid}:{thread_id}"[:180],
            "scope": scope,
            "platform": "bilibili",
            "user_id": user_id or "",
            "group_id": group_id,
            # 不传 bot_id：bot_self 的归属判定会因为它误拒。
            "message_text": "",
            "strict_session_only": False,
        }

    @staticmethod
    def _is_bili_item(item: dict[str, Any]) -> bool:
        source = str(item.get("source_plugin") or "").lower()
        return any(source.startswith(prefix) for prefix in BILI_SOURCE_PREFIXES)

    def _render_items(
        self,
        items: list[dict[str, Any]],
        *,
        allow_private_pair: bool,
        budget: int,
        limit: int,
        exclude_types: set[str] | None = None,
    ) -> list[str]:
        """把中枢的检索结果压成提示词里的几行短句。读不到就返回空列表。

        过滤只在这里做：写的时候一律原样入库，读的时候才决定给不给看。
        """
        lines: list[str] = []
        used = 0
        for item in items or []:
            visibility = str(item.get("visibility") or "")
            if visibility == "private_pair" and not allow_private_pair:
                continue
            if visibility not in {"bot_self", "shareable", "private_pair", "group_public"}:
                continue
            if exclude_types and str(item.get("memory_type") or "") in exclude_types:
                continue
            content = _clean(item.get("content"), 400)
            if not content:
                continue
            if used + len(content) > budget and lines:
                break
            used += len(content)
            stamp = _clean(item.get("occurred_at"), 16)
            lines.append(f"- {stamp} {content}".strip() if stamp else f"- {content}")
            if len(lines) >= limit:
                break
        return lines

    async def recall_for_bili(
        self,
        query: str,
        *,
        channel: str = "comment",
        user_id: str = "",
        oid: str = "",
        thread_id: str = "",
    ) -> str:
        """B站 回复前调用：返回一段可以直接拼进提示词的文字，读不到就返回空串。

        可见性完全由中枢决定，这里只做两件事：
          - 评论区当作公开场合（scope=group），私信才当私聊；
          - 私信里只有老大本人会被映射成 QQ 身份，从而读到他自己的私聊记忆。
        """
        if not self.enabled or not self._bool("enable_bili_recall", True):
            return ""
        service = self._memory_service()
        if service is None:
            return ""
        try:
            text = _clean(query, 600)
            channel_key = str(channel or "comment").strip().lower()
            if channel_key == "private":
                scope = "private"
                reader = self.map_bili_user(user_id)
            else:
                scope = "group"
                reader = _clean(user_id, 40)

            items = await service.bridge_search(
                text or "今天",
                session_context=self._bili_context(
                    scope=scope, user_id=reader, target=channel_key, oid=_clean(oid, 40), thread_id=_clean(thread_id, 60)
                ),
                top_k=self._int("bili_recall_top_k", 5, 1, 12) * 2,
            )
            if not items:
                return ""

            is_private = channel_key == "private"
            allow_private_pair = is_private and reader == self.owner_qq and bool(self.owner_qq)

            budget = self._int("bili_recall_max_chars", 700, 100, 2000)
            lines: list[str] = []
            used = 0
            for item in items:
                visibility = str(item.get("visibility") or "")
                if visibility == "private_pair" and not allow_private_pair:
                    continue
                if visibility not in {"bot_self", "shareable", "private_pair"}:
                    continue
                content = _clean(item.get("content"), 400)
                if not content:
                    continue
                if used + len(content) > budget and lines:
                    break
                used += len(content)
                stamp = _clean(item.get("occurred_at"), 16)
                lines.append(f"- {stamp} {content}".strip() if stamp else f"- {content}")
                if len(lines) >= self._int("bili_recall_top_k", 5, 1, 12):
                    break
            if not lines:
                return ""
            self.read_count += 1
            self.last_read_at = time.time()
            head = (
                "【你记得的其他场合（来自你自己的记忆，不是这次对话）】\n"
                "这些是你真实经历过的，可以像回忆一样提起来；"
                "但如果这条是公开评论区，**不要说出任何 QQ 私聊里的内容、称呼或别人说过的原话**。\n"
            )
            if not is_private:
                head += "这是公开场合，只能用你自己的事和公开过的内容。\n"
            if self.debug:
                logger.info(f"[生活桥] B站 {channel_key} 注入 {len(lines)} 条")
            return head + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"recall_for_bili: {exc}"
            logger.warning(f"[生活桥] B站 侧读记忆失败：{exc}")
            return ""

    async def remember_bili(
        self,
        content: str,
        *,
        kind: str = "watch",
        visibility: str = "bot_self",
        bvid: str = "",
        occurred_at: str = "",
        tags: list[str] | None = None,
        important: float = 0.6,
        reality: str = "bot_action",
    ) -> str:
        """B站 侧发生的事写进中枢。返回 memory_id，失败返回空串。

        ``visibility``：
          - ``bot_self``  她自己的经历，所有端可读（看片、回评论、发动态）
          - ``shareable`` 明确可以到处说的
          - ``private_pair`` 只给当事人（B站 私信里对方说的话）

        ``reality``：这条经历有多"实"。
          - ``bot_action`` 真做过（看了视频、发了动态）
          - ``media_index`` 只拿到文字资料（标题/简介/字幕），没看画面
          - ``observed`` 只看到别人的东西
        写不准的话她会吹牛说"我看了"，问细节就露馅。
        """
        if not self.enabled:
            return ""
        service = self._memory_service()
        if service is None:
            return ""
        body = _clean(content, MAX_CONTENT)
        if not body:
            return ""
        visibility = visibility if visibility in {"bot_self", "shareable", "private_pair"} else "bot_self"
        stamp = _clean(occurred_at, 60) or datetime.now().isoformat()
        # 有 BV 就用 BV 做幂等键：同一个视频反复入库只留一条。
        if bvid:
            memory_id = f"bili:{kind}:{bvid}"
        else:
            memory_id = f"bili:{kind}:{stamp}:{body[:16]}"
        try:
            await service.record_external_event(
                content=body,
                memory_type=f"bilibili_{kind}"[:40],
                visibility=visibility,
                scope="private" if visibility == "private_pair" else "unknown",
                platform="bilibili",
                session_id="bilibili:activity",
                reality_level=reality or "bot_action",
                lifecycle="stable_memory",
                confidence=0.88,
                importance=important,
                review_status="auto",
                tags=[t for t in (tags or ["B站", kind]) if t][:8],
                metadata={"bvid": bvid, "life_bridge": PROVIDER_ID},
                source_plugin="bilibili",
                occurred_at=stamp,
                memory_id=memory_id,
            )
            self.write_count += 1
            self.last_write_at = time.time()
            if self.debug:
                logger.info(f"[生活桥] B站 事件已入中枢：{kind} / {body[:40]}")
            return memory_id
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"remember_bili: {exc}"
            logger.warning(f"[生活桥] 写 B站 事件失败：{exc}")
            return ""

    async def has_memory(self, memory_id: str) -> bool:
        """中枢里有没有这条记忆。给「别重复写」用。"""
        mid = _clean(memory_id, 160)
        if not mid:
            return False
        service = self._memory_service()
        if service is None:
            return False
        try:
            store = getattr(service, "store", None)
            getter = getattr(store, "get_memory", None)
            if not callable(getter):
                return False
            return await getter(mid) is not None
        except Exception:
            return False

    # ══════════════════════════════════════════
    #  记忆信箱 · 写入（桌宠 / DSH）
    # ══════════════════════════════════════════

    def _is_noise(self, text: str) -> bool:
        """桌宠的 system 里会混进 DSH 状态播报，那不是她经历的对话，别记。"""
        raw = self._cfg("pet_noise_markers", []) or []
        if not isinstance(raw, list):
            raw = [raw]
        haystack = text or ""
        return any(str(marker) and str(marker) in haystack for marker in raw)

    def _pet_should_skip(self, scenario: str, user_text: str) -> str:
        """返回跳过原因，空串表示可以写。"""
        if len(user_text) < self._int("pet_min_user_chars", 4, 1, 200):
            return "too_short"
        if self._is_noise(user_text):
            return "noise"
        now = time.time()
        # 同一句话在这个窗口里写过就不写第二次（桌宠重发、点两下都会触发）
        seen_ttl = self._int("pet_dedupe_seconds", 1800, 0, 86400)
        key = hashlib.sha1(f"{scenario}|{user_text[:120]}".encode("utf-8")).hexdigest()
        last = self._pet_seen.get(key, 0.0)
        if seen_ttl and now - last < seen_ttl:
            return "duplicate"
        # 场景级节流：连着聊天不至于把中枢刷满
        gap = self._int(
            "pet_min_interval_seconds" if scenario == "chat" else "pet_vision_min_interval_seconds",
            120 if scenario == "chat" else 20,
            0,
            86400,
        )
        if gap and now - self._pet_last_write.get(scenario, 0.0) < gap:
            return "throttled"
        # 记下来，真正写成功也还会再更新一次时间
        if seen_ttl:
            self._pet_seen[key] = now
        return ""

    async def ingest_pet(self, payload: dict[str, Any]) -> dict[str, Any]:
        """收下桌宠网关推来的一轮对话。

        可见性按场景分两种，这是隐私边界所在，别为了"方便"改：
          - vision（识屏）→ bot_self：她亲眼看到的桌面，别的端也能提
          - chat  （聊天）→ private_pair：只对老大本人，B站 评论区读不到
        """
        if not self.enabled or not self._bool("enable_pet_ingest", True):
            return {"ok": False, "state": "disabled"}
        service = self._memory_service()
        if service is None:
            return {"ok": False, "state": "memory_missing"}
        scenario = _clean(payload.get("scenario"), 20).lower() or "chat"
        if scenario not in PET_SCENARIO_KIND:
            scenario = "chat"
        user_text = _squash(payload.get("user"), 1200)
        reply = _squash(payload.get("assistant"), 2000)
        if not reply:
            return {"ok": False, "state": "no_reply"}
        reason = self._pet_should_skip(scenario, user_text)
        if reason:
            self.pet_skip_count += 1
            if self.debug:
                logger.info(f"[生活桥] 桌宠 {scenario} 跳过（{reason}）：{user_text[:30]}")
            return {"ok": False, "state": reason}

        try:
            epoch = int(payload.get("ts") or time.time())
        except (TypeError, ValueError):
            epoch = int(time.time())
        occurred_at = datetime.fromtimestamp(epoch).isoformat()
        memory_type = PET_SCENARIO_KIND[scenario]

        if scenario == "vision":
            body = f"【电脑桌面·识屏】{user_text}｜我：{reply}"
        else:
            body = f"【电脑桌面·聊天】老大：{user_text}｜我：{reply}"
        body = _clean(body, MAX_CONTENT)

        try:
            if scenario == "vision":
                # 她自己的眼睛看到的，挂在 bot_self 上，所有端都能提起来。
                memory_id = f"pet:vision:{epoch}"
                await service.record_external_event(
                    content=body,
                    memory_type=memory_type,
                    visibility="bot_self",
                    scope="private",
                    platform="desktop_pet",
                    session_id="pet:desktop",
                    reality_level="bot_action",
                    lifecycle="stable_memory",
                    confidence=0.86,
                    importance=self._float("pet_vision_importance", 0.55, 0.1, 1.0),
                    review_status="auto",
                    tags=["桌面", "识屏"],
                    metadata={"life_bridge": PROVIDER_ID, "pet_scenario": scenario},
                    source_plugin="paopao_pet",
                    occurred_at=occurred_at,
                    memory_id=memory_id,
                )
            else:
                # 桌面上聊的天属于私聊，绑到老大身上：QQ 私聊和 B站 私信能读到，
                # 群里和评论区读不到。走中枢那个更窄的 external_memory 接口。
                result = await service.record_external_memory(
                    user_id=self.owner_qq,
                    content=body,
                    memory_type=memory_type,
                    source_plugin="paopao_pet",
                    occurred_at=occurred_at,
                    idempotency_key=f"pet:{epoch}",
                    memory_id=f"pet:chat:{epoch}",
                    importance=self._float("pet_chat_importance", 0.5, 0.1, 1.0),
                    confidence=0.82,
                    tags=["桌面", "聊天"],
                    metadata={"life_bridge": PROVIDER_ID, "pet_scenario": scenario},
                )
                memory_id = str((result or {}).get("memory_id") or "")
                if not (result or {}).get("ok"):
                    raise RuntimeError(str((result or {}).get("error_code") or "record_failed"))

            self._pet_last_write[scenario] = time.time()
            self.pet_write_count += 1
            self.write_count += 1
            self.last_write_at = time.time()
            if self.debug:
                logger.info(f"[生活桥] 桌宠 {scenario} 已入中枢：{body[:40]}")
            return {"ok": True, "state": "stored", "memory_id": memory_id}
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"ingest_pet: {exc}"
            logger.warning(f"[生活桥] 写桌宠记忆失败：{exc}")
            return {"ok": False, "state": "error", "error": str(exc)}

    async def remember_external(
        self,
        content: str,
        *,
        source: str = "paopao_dsh",
        visibility: str = "private_pair",
        memory_type: str = "dsh_chat",
        occurred_at: str = "",
        tags: list[str] | None = None,
        importance: float = 0.6,
        memory_id: str = "",
    ) -> dict[str, Any]:
        """任何本地进程（DSH）写一条记忆。私聊语义，绑到老大身上。"""
        if not self.enabled:
            return {"ok": False, "state": "disabled"}
        service = self._memory_service()
        if service is None:
            return {"ok": False, "state": "memory_missing"}
        body = _clean(content, MAX_CONTENT)
        if not body:
            return {"ok": False, "state": "empty"}
        stamp = _clean(occurred_at, 60) or datetime.now().isoformat()
        idem = _clean(memory_id, 120) or f"{source}:{stamp}:{body[:40]}"
        try:
            if visibility == "bot_self":
                mid = memory_id or f"{source}:self:{stamp}:{body[:16]}"
                await service.record_external_event(
                    content=body,
                    memory_type=memory_type,
                    visibility="bot_self",
                    scope="private",
                    platform=source,
                    session_id=f"{source}:activity",
                    reality_level="bot_action",
                    lifecycle="stable_memory",
                    confidence=0.86,
                    importance=importance,
                    review_status="auto",
                    tags=[t for t in (tags or ["DSH"]) if t][:8],
                    metadata={"life_bridge": PROVIDER_ID},
                    source_plugin=source,
                    occurred_at=stamp,
                    memory_id=mid,
                )
            else:
                result = await service.record_external_memory(
                    user_id=self.owner_qq,
                    content=body,
                    memory_type=memory_type,
                    source_plugin=source,
                    occurred_at=stamp,
                    idempotency_key=idem,
                    memory_id=memory_id,
                    importance=importance,
                    confidence=0.82,
                    tags=[t for t in (tags or ["DSH"]) if t][:8],
                    metadata={"life_bridge": PROVIDER_ID},
                )
                if not (result or {}).get("ok"):
                    return {"ok": False, "state": "rejected", "error": str((result or {}).get("error_code") or "")}
                mid = str((result or {}).get("memory_id") or "")
            self.dsh_write_count += 1
            self.write_count += 1
            self.last_write_at = time.time()
            return {"ok": True, "state": "stored", "memory_id": mid}
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"remember_external: {exc}"
            logger.warning(f"[生活桥] 写外部记忆失败：{exc}")
            return {"ok": False, "state": "error", "error": str(exc)}

    # ══════════════════════════════════════════
    #  记忆信箱 · 读取（桌宠 / DSH）
    # ══════════════════════════════════════════

    async def recall_private(
        self,
        query: str,
        *,
        limit: int = 5,
        max_chars: int = 800,
        session_id: str = "pet:desktop",
        platform: str = "desktop_pet",
        head: str = "",
    ) -> str:
        """按私聊语义召回一段文字（桌宠桌面 / DSH 都用它）。

        scope=private + user_id=老大 → 能读到 private_pair（和老大本人的私事）、
        bot_self（她自己的经历）、shareable（可以到处说的）、group_public（群里的公开话）。
        读不到 group_public 之外任何别的东西，所以评论区那套边界不受影响。
        """
        if not self.enabled or not self._bool("enable_pet_recall", True):
            return ""
        service = self._memory_service()
        if service is None:
            return ""
        text = _clean(query, 600) or "今天"
        try:
            items = await service.bridge_search(
                text,
                session_context={
                    "session_id": session_id[:180],
                    "scope": "private",
                    "platform": platform,
                    "user_id": self.owner_qq,
                    "group_id": "",
                    "message_text": "",
                    "strict_session_only": False,
                },
                top_k=max(limit * 3, 8),
            )
            if not items:
                return ""
            exclude = set()
            raw_exclude = self._cfg("pet_exclude_memory_types", []) or []
            if isinstance(raw_exclude, list):
                exclude = {str(x) for x in raw_exclude if x}
            lines = self._render_items(
                items,
                allow_private_pair=bool(self.owner_qq),
                budget=max_chars,
                limit=limit,
                exclude_types=exclude,
            )
            if not lines:
                return ""
            self.read_count += 1
            self.last_read_at = time.time()
            if self.debug:
                logger.info(f"[生活桥] {platform} 注入 {len(lines)} 条")
            block = head or (
                "【你记得的其他场合（同一份记忆，不是这次对话）】\n"
                "这些是你真实经历过的：QQ 里聊过的、B站 上做过的、桌面上看到过的。\n"
                "可以像回忆一样自然提起来，别念清单，也别说「记忆里写着」。\n"
            )
            return block + "\n".join(lines)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"recall_private: {exc}"
            logger.warning(f"[生活桥] {platform} 读记忆失败：{exc}")
            return ""

    # ══════════════════════════════════════════
    #  记忆信箱 · HTTP（只监听 127.0.0.1）
    # ══════════════════════════════════════════

    def _auth_ok(self, request) -> bool:
        token = _clean(self._cfg("http_token", ""), 200)
        if not token:
            return True
        return request.headers.get("X-Paopao-Token", "") == token

    async def _start_http(self) -> None:
        if not self._bool("enable_http", True):
            logger.info("[生活桥] 记忆信箱未启用（enable_http=false），桌宠和 DSH 不会同步记忆")
            return
        if web is None:
            logger.warning("[生活桥] 没有 aiohttp，记忆信箱起不来，桌宠记忆不会同步")
            return
        host = _clean(self._cfg("http_host", "127.0.0.1"), 60) or "127.0.0.1"
        port = self._int("http_port", 9889, minimum=1, maximum=65535)
        app = web.Application(client_max_size=1 << 20)
        app.router.add_get("/health", self._http_health)
        app.router.add_post("/pet/inbox", self._http_pet_inbox)
        app.router.add_post("/memory/recall", self._http_recall)
        app.router.add_post("/memory/remember", self._http_remember)
        runner = web.AppRunner(app, access_log=None)
        try:
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()
        except OSError as exc:
            logger.warning(f"[生活桥] 记忆信箱端口 {host}:{port} 起不来（{exc}），桌宠记忆不会同步")
            try:
                await runner.cleanup()
            except Exception:
                pass
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[生活桥] 记忆信箱启动失败：{exc}")
            return
        self._http_runner = runner
        self._http_site = site
        logger.info("[LifeBridge] 记忆信箱已监听 http://%s:%d", host, port)

    async def _stop_http(self) -> None:
        runner = self._http_runner
        self._http_runner = None
        self._http_site = None
        if runner is None:
            return
        try:
            await runner.cleanup()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[生活桥] 记忆信箱关闭失败：{exc}")

    async def _read_json(self, request) -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    async def _http_health(self, request):  # noqa: ANN001
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        return web.json_response(self.health())

    async def _http_pet_inbox(self, request):  # noqa: ANN001
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        payload = await self._read_json(request)
        if not payload:
            return web.json_response({"ok": False, "error": "bad json"}, status=400)
        return web.json_response(await self.ingest_pet(payload))

    async def _http_recall(self, request):  # noqa: ANN001
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        payload = await self._read_json(request)
        query = _clean(payload.get("query"), 600)
        if not query:
            return web.json_response({"ok": False, "error": "query required"}, status=400)
        source = _clean(payload.get("source"), 40) or "desktop_pet"
        try:
            asked = int(payload.get("limit") or 0)
        except (TypeError, ValueError):
            asked = 0
        limit = asked if asked > 0 else self._int("pet_recall_top_k", 5, 1, 12)
        text = await self.recall_private(
            query,
            limit=min(limit, 12),
            max_chars=self._int("pet_recall_max_chars", 800, 100, 4000),
            session_id=f"{source}:desktop"[:180],
            platform=source,
        )
        return web.json_response({"ok": bool(text), "text": text, "chars": len(text)})

    async def _http_remember(self, request):  # noqa: ANN001
        if not self._auth_ok(request):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        if not self._bool("enable_external_write", True):
            return web.json_response({"ok": False, "state": "disabled"}, status=403)
        payload = await self._read_json(request)
        content = _clean(payload.get("content"), MAX_CONTENT)
        if not content:
            return web.json_response({"ok": False, "error": "content required"}, status=400)
        tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        try:
            importance = float(payload.get("importance", 0.6))
        except (TypeError, ValueError):
            importance = 0.6
        result = await self.remember_external(
            content,
            source=_clean(payload.get("source"), 40) or "paopao_dsh",
            visibility=_clean(payload.get("visibility"), 20) or "private_pair",
            memory_type=_clean(payload.get("memory_type"), 40) or "dsh_chat",
            occurred_at=_clean(payload.get("occurred_at"), 60),
            tags=[_clean(t, 30) for t in tags if _clean(t, 30)][:8],
            importance=max(0.1, min(1.0, importance)),
            memory_id=_clean(payload.get("memory_id"), 120),
        )
        return web.json_response(result)

    def health(self) -> dict[str, Any]:
        """给文档里的健康检查用。"""
        return {
            "ok": self._memory_service() is not None,
            "version": PLUGIN_VERSION,
            "enabled": self.enabled,
            "memory_service": self._memory_service() is not None,
            "companion_provider": self._registration is not None,
            "mailbox_listening": self._http_site is not None,
            "writes": self.write_count,
            "reads": self.read_count,
            "pet_writes": self.pet_write_count,
            "pet_skips": self.pet_skip_count,
            "external_writes": self.dsh_write_count,
            "last_write_at": self.last_write_at,
            "last_read_at": self.last_read_at,
            "last_error": self.last_error,
        }
