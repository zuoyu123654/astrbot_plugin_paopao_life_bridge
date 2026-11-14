#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""泡泡记忆信箱 · 命令行客户端

给「不在 AstrBot 里」的地方用 —— 主要是 DSH 这边的泡泡。
桌宠走的是网关，不需要这个；QQ 和 B站 走插件内部，也不需要。

    python paopao_memory.py health
    python paopao_memory.py recall "老大最近在忙什么" --limit 5
    python paopao_memory.py remember "老大说想吃鱼" --tags 吃的,约定 --importance 0.7
    python paopao_memory.py remember "我今天在 DSH 里给她修了记忆" --self

    # 模拟桌宠推一轮（调试用）
    python paopao_memory.py inbox --scenario vision --user "屏幕上是..." --assistant "哇"

只用标准库。AstrBot 没开 / 桥没起时，会清晰地报错而不是卡住。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:9889"


def _post(url: str, payload: dict, token: str = "", timeout: float = 15.0) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["X-Paopao-Token"] = token
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _get(url: str, token: str = "", timeout: float = 10.0) -> dict:
    headers = {}
    if token:
        headers["X-Paopao-Token"] = token
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _die(msg: str) -> None:
    print(f"× {msg}", file=sys.stderr)
    print("  AstrBot 起了吗？生活桥的 enable_http 开了吗？端口对吗？", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser(description="泡泡记忆信箱客户端")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"信箱地址，默认 {DEFAULT_BASE}")
    ap.add_argument("--token", default="", help="http_token 设了才需要")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health", help="看信箱状态")

    p_recall = sub.add_parser("recall", help="按私聊语义查记忆")
    p_recall.add_argument("query")
    p_recall.add_argument("--limit", type=int, default=5)
    p_recall.add_argument("--source", default="paopao_dsh")

    p_rem = sub.add_parser("remember", help="写一条记忆")
    p_rem.add_argument("content")
    p_rem.add_argument("--tags", default="", help="逗号分隔")
    p_rem.add_argument("--importance", type=float, default=0.6)
    p_rem.add_argument("--memory-type", default="dsh_chat")
    p_rem.add_argument(
        "--self",
        action="store_true",
        help="记成她自己的经历（bot_self，所有端可读）；不加就是私聊（private_pair）",
    )

    p_inbox = sub.add_parser("inbox", help="模拟桌宠推一轮对话")
    p_inbox.add_argument("--scenario", default="chat", choices=["chat", "vision"])
    p_inbox.add_argument("--user", required=True)
    p_inbox.add_argument("--assistant", required=True)

    args = ap.parse_args()
    base = args.base.rstrip("/")

    try:
        if args.cmd == "health":
            print(json.dumps(_get(f"{base}/health", args.token), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "recall":
            data = _post(
                f"{base}/memory/recall",
                {
                    "query": args.query,
                    "source": args.source,
                    "limit": args.limit,
                },
                args.token,
            )
            text = str(data.get("text") or "").strip()
            print(text if text else "（没查到相关的记忆）")
            return 0

        if args.cmd == "remember":
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]
            data = _post(
                f"{base}/memory/remember",
                {
                    "content": args.content,
                    "source": "paopao_dsh",
                    "memory_type": args.memory_type,
                    "visibility": "bot_self" if args.self else "private_pair",
                    "importance": args.importance,
                    "tags": tags,
                    "occurred_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                args.token,
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data.get("ok") else 1

        if args.cmd == "inbox":
            data = _post(
                f"{base}/pet/inbox",
                {
                    "scenario": args.scenario,
                    "user": args.user,
                    "assistant": args.assistant,
                    "ts": int(time.time()),
                },
                args.token,
            )
            print(json.dumps(data, ensure_ascii=False, indent=2))
            return 0 if data.get("ok") else 1
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        _die(f"HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        _die(f"连不上 {base}（{exc.reason}）")
    except Exception as exc:  # noqa: BLE001
        _die(str(exc))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
