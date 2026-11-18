#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONTAINER = "new-api"
# 本机数据库路径（容器挂载到宿主机）
LOCAL_DB_PATH = "/root/new-api-data/one-api.db"
# 容器内路径（保留用于引用）
DB_PATH = "/data/one-api.db"
DEFAULT_MODEL = "claude-opus-4-6"
DEFAULT_GROUP = "default"
DEFAULT_MESSAGE = "say OK"
DEFAULT_MAX_TOKENS = 8
DEFAULT_FAST_LIMIT = 6
DEFAULT_PARALLELISM = 4
DEFAULT_HTTP_TIMEOUT = 8
FULL_HTTP_TIMEOUT = 15
STATE_PATH = Path(os.getenv("MULERUN_WATCH_STATE", "/tmp/new-api-mulerun-pool-watch.state.json"))
LOCK_PATH = Path("/tmp/new-api-mulerun-pool-watch.lock")
BALANCE_RE = re.compile(r"Available balance:\s*([0-9]+(?:\.[0-9]+)?)\s*credits", re.I)
MIN_REQUIRED_RE = re.compile(r"Minimum required:\s*([0-9]+(?:\.[0-9]+)?)\s*credits", re.I)
MULERUN_ENDPOINT = "https://api.mulerun.com/v1/chat/completions"


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")



def run(cmd: List[str], check: bool = False, input_text: Optional[str] = None):
    p = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=True,
    )
    if check and p.returncode != 0:
        raise RuntimeError(
            f"command failed ({p.returncode}): {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}"
        )
    return p



def docker_exec(args: List[str], env: Optional[Dict[str, str]] = None, check: bool = False, container: str = CONTAINER):
    cmd = ["docker", "exec"]
    for k, v in (env or {}).items():
        cmd.extend(["-e", f"{k}={v}"])
    cmd.append(container)
    cmd.extend(args)
    return run(cmd, check=check)



def db_query(sql: str, db_path: str = LOCAL_DB_PATH, container: str = CONTAINER) -> List[str]:
    # 使用本机 sqlite3 替代容器内查询
    cmd = ["sqlite3", "-separator", "\t", db_path, sql]
    p = run(cmd, check=True)
    return [line for line in p.stdout.splitlines() if line.strip()]



def db_exec(sql: str, db_path: str = LOCAL_DB_PATH, container: str = CONTAINER):
    # 使用本机 sqlite3 替代容器内执行
    cmd = ["sqlite3", db_path, sql]
    return run(cmd, check=True)



def _safe_float(value: str) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        return None



def get_channels(model: str, db_path: str = DB_PATH, container: str = CONTAINER, match_like: bool = True) -> List[Dict[str, Any]]:
    where_clause = f"c.models LIKE '%{model}%'" if match_like else f"c.models = '{model}'"
    sql = f'''
SELECT c.id, c.name, c.status, c.priority, c.weight, c.auto_ban, c.key,
       IFNULL(a.enabled, 1) AS ability_enabled,
       IFNULL(c.balance, ''), IFNULL(c.balance_updated_time, ''), IFNULL(c.used_quota, 0)
FROM channels c
LEFT JOIN abilities a
  ON a.channel_id = c.id
 AND a.model = '{model}'
 AND a."group" = '{DEFAULT_GROUP}'
WHERE {where_clause}
ORDER BY c.id ASC;
'''
    rows: List[Dict[str, Any]] = []
    for line in db_query(sql, db_path=db_path, container=container):
        parts = line.split("\t")
        (
            cid,
            name,
            status,
            priority,
            weight,
            auto_ban,
            key,
            ability_enabled,
            balance,
            balance_updated_time,
            used_quota,
        ) = parts
        rows.append(
            {
                "id": int(cid),
                "name": name,
                "status": int(status),
                "priority": int(priority),
                "weight": int(weight),
                "auto_ban": int(auto_ban),
                "key": key,
                "ability_enabled": int(ability_enabled),
                "db_balance": None if balance == "" else _safe_float(balance),
                "balance_updated_time": None if balance_updated_time == "" else int(balance_updated_time),
                "used_quota": int(used_quota or 0),
            }
        )
    return rows



def parse_balance_fields(text: str) -> Tuple[Optional[float], Optional[float]]:
    m1 = BALANCE_RE.search(text or "")
    m2 = MIN_REQUIRED_RE.search(text or "")
    available = float(m1.group(1)) if m1 else None
    minimum = float(m2.group(1)) if m2 else None
    return available, minimum



def classify_probe_output(text: str) -> Tuple[str, str, Optional[float], Optional[float]]:
    available_balance, minimum_required = parse_balance_fields(text)
    lowered = (text or "").lower()
    
    # 尝试从 JSON 响应中提取 usage 信息
    detail = ""
    if '"usage"' in text or '"choices"' in text:
        try:
            data = json.loads(text)
            if "usage" in data:
                u = data["usage"]
                prompt_tok = u.get("prompt_tokens", u.get("input_tokens", 0))
                comp_tok = u.get("completion_tokens", u.get("output_tokens", 0))
                detail = f"tokens:{prompt_tok}+{comp_tok}"
        except Exception:
            pass
    
    # 如果没有提取到 usage，显示简化信息
    if not detail:
        detail = (text or "").strip().replace("\n", " ")[:60]
    
    if "available balance:" in lowered and available_balance is not None:
        if minimum_required is not None and available_balance < minimum_required:
            return "insufficient_balance", detail, available_balance, minimum_required
        return "ok", detail, available_balance, minimum_required
    if "minimum required:" in lowered or "insufficient" in lowered or "402" in lowered:
        return "insufficient_balance", detail, available_balance, minimum_required
    if "invalid" in lowered or "unauthorized" in lowered or "forbidden" in lowered:
        return "bad_key", detail, available_balance, minimum_required
    if "timeout" in lowered or "temporarily" in lowered or "connection reset" in lowered:
        return "transient_error", detail, available_balance, minimum_required
    if '"id"' in lowered and '"choices"' in lowered:
        return "ok", detail, available_balance, minimum_required
    return "unknown_error", detail, available_balance, minimum_required



def probe_key(key: str, model: str, container: str = CONTAINER, max_tokens: int = 8, prompt: str = DEFAULT_MESSAGE, http_timeout: int = 8):
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        }
    )
    # 使用本机 wget 替代容器内调用
    cmd = [
        "wget", "-qO-", "--timeout=" + str(http_timeout),
        "--header=Authorization: Bearer " + key,
        "--header=Content-Type: application/json",
        "--post-data=" + body,
        MULERUN_ENDPOINT
    ]
    p = run(cmd, check=False)
    text = (p.stdout or "") + ("\n" + p.stderr if p.stderr else "")
    classification, detail, available_balance, minimum_required = classify_probe_output(text)
    return {
        "classification": classification,
        "detail": detail,
        "available_balance": available_balance,
        "minimum_required": minimum_required,
        "raw_excerpt": detail,
    }



def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}



def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")



def record_auto_disabled(channel_id: int, model: str, reason: str, name: str = ""):
    state = load_state()
    auto_disabled = state.setdefault("auto_disabled", {})
    bucket = auto_disabled.setdefault(model, {})
    bucket[str(channel_id)] = {
        "reason": reason,
        "disabled_at": int(time.time()),
        "name": name,
    }
    save_state(state)



def clear_auto_disabled(channel_id: int, model: str):
    state = load_state()
    auto_disabled = state.get("auto_disabled", {})
    bucket = auto_disabled.get(model, {})
    if str(channel_id) in bucket:
        del bucket[str(channel_id)]
        if not bucket:
            auto_disabled.pop(model, None)
        save_state(state)



def get_daily_reset_targets(channels, model: str):
    state = load_state()
    bucket = state.get("auto_disabled", {}).get(model, {})
    targets = []
    for ch in channels:
        if ch["status"] != 3:
            continue
        meta = bucket.get(str(ch["id"]))
        if not meta:
            continue
        if meta.get("reason") != "insufficient_balance":
            continue
        targets.append((ch, meta))
    return targets



def disable_channel(channel_id: int, model: str, reason: str, dry_run: bool = False, db_path: str = DB_PATH, container: str = CONTAINER, name: str = ""):
    sql = f'''
BEGIN;
UPDATE channels
   SET status = 3
 WHERE id = {channel_id}
   AND status = 1;
UPDATE abilities
   SET enabled = 0
 WHERE channel_id = {channel_id}
   AND model = '{model}'
   AND "group" = '{DEFAULT_GROUP}';
COMMIT;
'''
    if dry_run:
        log_msg = f"DRY-RUN disable channel={channel_id} reason={reason}"
        print(f"[{now_iso()}] {log_msg}")
        return True
    db_exec(sql, db_path=db_path, container=container)
    record_auto_disabled(channel_id, model, reason, name=name)
    print(f"[{now_iso()}] AUTO-DISABLED channel={channel_id} reason={reason}")
    return True



def reenable_channel(channel_id: int, model: str, dry_run: bool = False, db_path: str = DB_PATH, container: str = CONTAINER):
    sql = f'''
BEGIN;
UPDATE channels
   SET status = 1
 WHERE id = {channel_id}
   AND status = 3;
UPDATE abilities
   SET enabled = 1
 WHERE channel_id = {channel_id}
   AND model = '{model}'
   AND "group" = '{DEFAULT_GROUP}';
COMMIT;
'''
    if dry_run:
        print(f"[{now_iso()}] DRY-RUN re-enable channel={channel_id}")
        return True
    db_exec(sql, db_path=db_path, container=container)
    clear_auto_disabled(channel_id, model)
    print(f"[{now_iso()}] AUTO-REENABLED channel={channel_id}")
    return True



def send_alert(message_text: str, alert_channel: str, alert_account: str, alert_target: str):
    if not (alert_channel and alert_target):
        return
    cmd = [
        "openclaw", "message", "send",
        "--channel", alert_channel,
        "--target", alert_target,
        "--message", message_text,
    ]
    if alert_account:
        cmd.extend(["--account", alert_account])
    p = run(cmd)
    if p.returncode != 0:
        print(f"[{now_iso()}] ALERT_SEND_FAILED rc={p.returncode} detail={(p.stderr or p.stdout).strip()[:200]}")
    else:
        print(f"[{now_iso()}] ALERT_SENT")



def pick_balance_targets(channels, sample: int, state_key: str):
    candidates = [c for c in channels if c["status"] != 2]
    if not candidates:
        return [], 0
    state = load_state()
    cursor = int(state.get(state_key, 0)) % len(candidates)
    n = min(sample, len(candidates))
    picked = [candidates[(cursor + i) % len(candidates)] for i in range(n)]
    state[state_key] = (cursor + n) % len(candidates)
    save_state(state)
    return picked, cursor



def summarize_counts(items):
    summary = {}
    for item in items:
        summary[item["classification"]] = summary.get(item["classification"], 0) + 1
    return summary



def sort_report_item(item):
    if item["available_balance"] is not None:
        return (0, float(item["available_balance"]), item["id"])
    if item["probe_classification"] == "ok":
        floor = item["minimum_required"] if item["minimum_required"] is not None else 0.1
        return (1, float(floor), item["id"])
    if item["probe_classification"] == "insufficient_balance":
        return (2, 0.0, item["id"])
    if item["probe_classification"] == "bad_key":
        return (3, 999999.0, item["id"])
    return (4, 999999.0, item["id"])



def enrich_report_rows(rows, model, container, max_tokens, prompt, http_timeout, parallelism):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    enriched = []

    def work(row):
        probe = probe_key(
            key=row["key"],
            model=model,
            container=container,
            max_tokens=max_tokens,
            prompt=prompt,
            http_timeout=http_timeout,
        )
        available_balance = probe["available_balance"]
        minimum_required = probe["minimum_required"]
        if available_balance is not None:
            remaining_display = str(available_balance).rstrip("0").rstrip(".") if "." in str(available_balance) else str(available_balance)
            remaining_kind = "exact"
        elif probe["classification"] == "ok":
            floor = minimum_required if minimum_required is not None else 0.1
            remaining_display = f">={floor}（精确值未知）"
            remaining_kind = "lower_bound"
        else:
            remaining_display = "unknown"
            remaining_kind = "unknown"
        db_state = "enabled" if row["status"] == 1 and row["ability_enabled"] == 1 else "auto-disabled"
        item = {
            **row,
            "probe_classification": probe["classification"],
            "probe_detail": probe["detail"],
            "available_balance": available_balance,
            "minimum_required": minimum_required,
            "remaining_display": remaining_display,
            "remaining_kind": remaining_kind,
            "db_state": db_state,
            "raw_excerpt": probe["raw_excerpt"],
        }
        item["sort_key"] = sort_report_item(item)
        return item

    with ThreadPoolExecutor(max_workers=max(1, parallelism)) as pool:
        futures = [pool.submit(work, row) for row in rows]
        for future in as_completed(futures):
            enriched.append(future.result())

    return sorted(enriched, key=lambda x: x["sort_key"])



def build_report_summary(items, scanned: int, total: int):
    counts = {}
    for item in items:
        counts[item["probe_classification"]] = counts.get(item["probe_classification"], 0) + 1
    exact_known = sum(1 for item in items if item["remaining_kind"] == "exact")
    return {
        "total": total,
        "scanned": scanned,
        "classifications": counts,
        "exact_balance_known": exact_known,
        "exact_balance_unknown": len(items) - exact_known,
    }



def format_report_markdown(items, summary, model: str, fast_mode: bool):
    counts = summary["classifications"]
    lines = []
    title = f"## {model} 号池"
    if fast_mode:
        title += "（快速模式）"
    lines.append(title)
    lines.append("")
    lines.append("| 总数 | 已扫描 | 正常 | 余额不足 | 坏 Key | 瞬时错误 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    lines.append(
        f"| {summary['total']} | {summary['scanned']} | {counts.get('ok', 0)} | {counts.get('insufficient_balance', 0)} | {counts.get('bad_key', 0)} | {counts.get('transient_error', 0) + counts.get('unknown_error', 0)} |"
    )
    lines.append("")
    lines.append("| ID | Channel | 剩余额度 | 状态 | DB | 备注 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for item in items:
        lines.append(
            f"| {item['id']} | `{item['name']}` | **{item['remaining_display']}** | `{item['probe_classification']}` | `{item['db_state']}` | {item['probe_detail']} |"
        )
    if fast_mode and summary["scanned"] < summary["total"]:
        lines.append("")
        lines.append(f"> 仅快速探测前 {summary['scanned']} / {summary['total']} 个 key；如需完整榜单，请使用 `--full`。")
    return "\n".join(lines)



def build_report_payload(model: str, fast_mode: bool, summary, items):
    return {
        "generated_at": now_iso(),
        "model": model,
        "fast_mode": fast_mode,
        "summary": summary,
        "items": items,
    }
