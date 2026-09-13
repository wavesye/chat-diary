"""Manage channel bindings locally without loading any LLM configuration."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from diary_agent.users import UserRegistry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="管理 chat-diary 内部用户和聊天渠道绑定")
    default_db = (
        os.getenv("CHANNEL_BINDINGS_DB", "").strip()
        or str(Path(os.getenv("DIARY_DB_PATH", "data/diary.sqlite")).expanduser().parent
               / "channel_users.sqlite")
    )
    parser.add_argument("--database", type=Path, default=Path(default_db),
                        help="绑定数据库（默认 CHANNEL_BINDINGS_DB）")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("local", help="获取旧单用户数据对应的内部 user_id")
    create = commands.add_parser("create", help="创建数据隔离的新用户")
    create.add_argument("--name", default="")
    commands.add_parser("list", help="列出内部用户及渠道绑定")
    bind = commands.add_parser("bind", help="将渠道身份绑定到已存在的内部用户")
    bind.add_argument("--user-id", required=True)
    bind.add_argument("--channel", required=True)
    bind.add_argument("--external-user-id", required=True)
    unbind = commands.add_parser("unbind", help="移除渠道绑定，保留内部用户数据")
    unbind.add_argument("--channel", required=True)
    unbind.add_argument("--external-user-id", required=True)
    args = parser.parse_args(argv)
    registry = UserRegistry(args.database)
    try:
        if args.command == "local":
            print(registry.local_user_id())
        elif args.command == "create":
            print(registry.create_user(args.name))
        elif args.command == "list":
            print(json.dumps({"users": registry.list_users(), "bindings": registry.bindings()},
                             ensure_ascii=False, indent=2))
        elif args.command == "bind":
            registry.bind(args.channel, args.external_user_id, args.user_id)
            print("绑定成功")
        elif args.command == "unbind":
            removed = registry.unbind(args.channel, args.external_user_id)
            print("已移除绑定" if removed else "绑定不存在")
    except ValueError as error:
        parser.error(str(error))
    finally:
        registry.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
