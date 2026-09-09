from __future__ import annotations

import argparse
import json

from diary_agent.config import Settings
from diary_agent.service import DiaryService


def main() -> None:
    parser = argparse.ArgumentParser(description="增量导入 Obsidian 历史日记")
    parser.add_argument("action", choices=("scan", "extract", "status", "search"))
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--max-batches", type=int, default=20)
    parser.add_argument("--batch-size", type=int, default=5)
    args = parser.parse_args()
    service = DiaryService(Settings.from_env())
    if args.action == "status":
        result = service.history_index.status()
    elif args.action == "search":
        if not args.query:
            parser.error("search 需要查询文字")
        result = [item.__dict__ for item in service.history_index.search(args.query, 10)]
    else:
        if not service.history_importer:
            raise SystemExit("请先在 .env 设置 OBSIDIAN_HISTORY_PATH")
        if args.action == "scan":
            result = service.history_importer.scan()
        else:
            result = service.history_importer.extract(args.max_batches, args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
