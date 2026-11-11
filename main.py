"""泡泡生活桥 —— 把「我会永远陪着你」的生活和「我会牢牢记住你」的记忆接成一份。

设计要点（改动前先读这一段）：

1. 桥不生产记忆。它只做两件翻译：
   - 陪伴插件的日记/梦境/主动消息  →  记忆中枢的 bot_self 记忆
   - 记忆中枢里她自己的经历        →  B站 侧回复时要看的"我记得的其他场合"
2. 写的时候决定可见性，读的时候只做过滤。**不要在写的时候脱敏**，否则别的端永远
   拿不回细节；也不要在读的时候放宽，否则评论区会说漏 QQ 的私事。
3. 所有跨端调用都包在 try/except 里。桥挂了只会让她"忘事"，不能让她不回话。
4. 幂等键用源记录的 memory_id，重放不会写两条。

配对的两个插件：
  - astrbot_plugin_private_companion（我会永远陪着你）→ 生活、日程、心情、日记
  - astrbot_plugin_memory_companion（我会牢牢记住你）→ 记忆、时间线、可见性
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "paopao_life_bridge"
PLUGIN_VERSION = "1.1.1"

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

MAX_CONTENT = 1200


def _clean(value: Any, limit: int = 200) -> str:
    return str(value or "").strip()[:limit]


@register(
    PLUGIN_NAME,
    "paopao",
    "生活桥：把陪伴插件的生活和记忆中枢接成一份，让 QQ / B站 / DSH 是同一个她。",
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

    def _int(self, key: str, default: int, minimum: int = 0, maximum: int = 50) -> int:
        try:
            value = int(self._cfg(key, default))
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
        try:
            has_memory = self._memory_service() is not None
        except Exception:
            has_memory = False
        logger.info(
            "[LifeBridge] self-check memory=%s companion=%s owner_qq=%s owner_bili=%s",
            "ok" if has_memory else "missing",
            "ok" if ok else "missing",
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

    def health(self) -> dict[str, Any]:
        """给文档里的健康检查用。"""
        return {
            "enabled": self.enabled,
            "memory_service": self._memory_service() is not None,
            "companion_provider": self._registration is not None,
            "writes": self.write_count,
            "reads": self.read_count,
            "last_write_at": self.last_write_at,
            "last_read_at": self.last_read_at,
            "last_error": self.last_error,
        }
