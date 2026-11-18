#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = WORKSPACE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mulerun_pool.new_api_common import (  # noqa: E402
    CONTAINER,
    DEFAULT_FAST_LIMIT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MESSAGE,
    DEFAULT_MODEL,
    DEFAULT_PARALLELISM,
    DEFAULT_HTTP_TIMEOUT,
    FULL_HTTP_TIMEOUT,
    LOCAL_DB_PATH,
    build_report_payload,
    build_report_summary,
    enrich_report_rows,
    format_report_markdown,
    get_channels,
    now_iso,
)


def parse_args():
    parser = argparse.ArgumentParser(description="Inspect local new-api Mulerun quota pool")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--db-path", default=LOCAL_DB_PATH)
    parser.add_argument("--container", default=CONTAINER)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--prompt", default=DEFAULT_MESSAGE)
    parser.add_argument("--full", action="store_true", help="Probe all keys instead of fast mode")
    parser.add_argument("--limit", type=int, default=DEFAULT_FAST_LIMIT, help="Fast-mode scan limit")
    parser.add_argument("--parallel", type=int, default=DEFAULT_PARALLELISM)
    parser.add_argument("--http-timeout", type=int, default=0, help="Override upstream probe timeout seconds")
    return parser.parse_args()



def main() -> int:
    args = parse_args()
    rows = get_channels(model=args.model, db_path=args.db_path, container=args.container, match_like=True)
    total = len(rows)
    if total == 0:
        payload = {
            "generated_at": now_iso(),
            "model": args.model,
            "summary": {"total": 0, "scanned": 0, "classifications": {}},
            "items": [],
        }
        if args.format == "json":
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"## {args.model} 号池\n\n| 总数 | 已扫描 | 正常 | 余额不足 | 坏 Key | 瞬时错误 |\n| --- | --- | --- | --- | --- | --- |\n| 0 | 0 | 0 | 0 | 0 | 0 |")
        return 0

    fast_mode = not args.full
    scan_limit = total if args.full else max(1, min(args.limit, total))
    target_rows = rows[:scan_limit]
    http_timeout = args.http_timeout or (FULL_HTTP_TIMEOUT if args.full else DEFAULT_HTTP_TIMEOUT)
    items = enrich_report_rows(
        target_rows,
        model=args.model,
        container=args.container,
        max_tokens=args.max_tokens,
        prompt=args.prompt,
        http_timeout=http_timeout,
        parallelism=args.parallel,
    )
    summary = build_report_summary(items, scanned=len(target_rows), total=total)
    payload = build_report_payload(args.model, fast_mode, summary, items)
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(format_report_markdown(items, summary, args.model, fast_mode=fast_mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
