#!/usr/bin/env python3
import argparse
import fcntl
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = WORKSPACE_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mulerun_pool.new_api_common import (  # noqa: E402
    CONTAINER,
    LOCAL_DB_PATH,
    DEFAULT_MODEL,
    LOCK_PATH,
    STATE_PATH,
    classify_probe_output,
    db_exec,
    db_query,
    disable_channel,
    get_channels,
    get_daily_reset_targets,
    load_state,
    pick_balance_targets,
    probe_key,
    reenable_channel,
    save_state,
    send_alert,
    summarize_counts,
)


def main():
    ap = argparse.ArgumentParser(description="Watch Mulerun key pool behind new-api and auto-disable bad/zero-balance channels.")
    ap.add_argument("--container", default=CONTAINER)
    ap.add_argument("--db-path", default=LOCAL_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--mode", choices=["balance", "both", "daily-reset"], default="balance")
    ap.add_argument("--sample", type=int, default=2, help="balance mode samples N channels per run (rotation cursor persisted)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-reenable-ok", action="store_true")
    ap.add_argument("--alert-channel", default="")
    ap.add_argument("--alert-account", default="")
    ap.add_argument("--alert-target", default="")
    args = ap.parse_args()

    lockf = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[watch] another run is active, exit")
        return 0

    channels = get_channels(args.model, db_path=args.db_path, container=args.container, match_like=False)
    if not channels:
        print(f"[watch] no channels found for model={args.model}")
        return 0

    print(f"[watch] start mode={args.mode} model={args.model} channels={len(channels)} dry_run={args.dry_run}")
    changes = 0

    if args.mode == "daily-reset":
        targets = get_daily_reset_targets(channels, args.model)
        print(f"[watch] daily-reset targets={[ch['id'] for ch, _meta in targets]}")
        for ch, meta in targets:
            changed = reenable_channel(ch["id"], args.model, args.dry_run, db_path=args.db_path, container=args.container)
            if changed:
                changes += 1
                if not args.dry_run:
                    send_alert(
                        f"🌅 new-api key 凌晨自动重启试用\nchannel={ch['id']} {ch['name']}\nlast_reason={meta.get('reason','unknown')}\nmodel={args.model}",
                        args.alert_channel,
                        args.alert_account,
                        args.alert_target,
                    )
        print(f"[watch] done changes={changes}")
        return 0

    targets, cursor = pick_balance_targets(channels, args.sample, f"balance_cursor:{args.model}")
    print(f"[watch] balance targets={[c['id'] for c in targets]} cursor_start={cursor}")
    bal_results = []
    for ch in targets:
        probe = probe_key(
            key=ch["key"],
            model=args.model,
            container=args.container,
            max_tokens=8,
            prompt="say OK",
            http_timeout=25,
        )
        classification = probe["classification"]
        detail = probe["detail"]
        bal_results.append({"channel": ch, "classification": classification, "detail": detail})
        print(f"[watch] BALANCE id={ch['id']} name={ch['name']} class={classification} detail={detail[:120]}")
        if classification in ("insufficient_balance", "bad_key") and ch["status"] == 1:
            changed = disable_channel(ch["id"], args.model, classification, args.dry_run, db_path=args.db_path, container=args.container, name=ch["name"])
            if changed:
                changes += 1
                if not args.dry_run:
                    send_alert(
                        f"⚠️ new-api key 已自动禁用\nchannel={ch['id']} {ch['name']}\nreason={classification}\ndetail={detail[:160]}",
                        args.alert_channel,
                        args.alert_account,
                        args.alert_target,
                    )
        elif classification == "ok" and not args.no_reenable_ok and ch["status"] == 3:
            changed = reenable_channel(ch["id"], args.model, args.dry_run, db_path=args.db_path, container=args.container)
            if changed:
                changes += 1
                if not args.dry_run:
                    send_alert(
                        f"✅ new-api key 已自动恢复\nchannel={ch['id']} {ch['name']}\nmodel={args.model}",
                        args.alert_channel,
                        args.alert_account,
                        args.alert_target,
                    )
    print(f"[watch] balance summary={summarize_counts(bal_results)}")
    print(f"[watch] done changes={changes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
