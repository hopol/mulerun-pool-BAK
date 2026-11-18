"""Outlook/Microsoft Graph email provider for MuleRun registration.

This module intentionally replaces the old domain-email worker path.
It exposes the small interface used by register.py: gen_email_async(), poll_code(),
check_worker_health(), log_step().
"""
import asyncio
import csv
import json
import os
import re
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import LOG_FILE, RUN_ID, VERIFICATION_TIMEOUT

CODE_RE = re.compile(r"\b(\d{6})\b")
ACCOUNTS_PATH = Path(os.getenv("MR_OUTLOOK_ACCOUNTS", "accounts.outlook.csv"))


def log_step(step, idx="", email="", **extra):
    row = {"ts": round(time.time(), 3), "run": RUN_ID, "step": step, "idx": idx, "email": email}
    row.update(extra)
    line = json.dumps(row, ensure_ascii=False)
    print(f"  📋 {step} | {extra}")
    try:
        Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_accounts(path: Path = ACCOUNTS_PATH):
    if not path.exists():
        raise FileNotFoundError(f"Outlook account file not found: {path}")
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        sample = f.read(4096)
        f.seek(0)
        if "," in sample.splitlines()[0]:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("email"):
                    rows.append(r)
        else:
            for line in f:
                line=line.strip()
                if not line or line.startswith("#"):
                    continue
                parts=[x.strip() for x in line.split("|")]
                if len(parts) >= 2:
                    rows.append({"email": parts[0], "password": parts[1], "status": parts[2] if len(parts)>2 else "pending"})
    return rows


def _write_accounts(rows, path: Path = ACCOUNTS_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["email", "password", "status", "invite_code", "api_key"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w=csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k:r.get(k,"") for k in fields})


async def gen_email_async() -> Tuple[Optional[str], Optional[str]]:
    """Reserve the next pending Outlook/Hotmail account."""
    rows = _read_accounts()
    for r in rows:
        status=(r.get("status") or "pending").strip().lower()
        if status in ("", "pending", "new"):
            r["status"] = "reserved"
            _write_accounts(rows)
            email=r["email"].strip()
            return email, email.split("@")[0]
    return None, None


async def poll_code(email: str, timeout: int = VERIFICATION_TIMEOUT) -> Optional[str]:
    """Poll Outlook mailbox for MuleRun verification code.

    The concrete Graph API implementation lives in providers.outlook_provider and
    expects config_outlook.json / OUTLOOK_CONFIG to be prepared by the operator.
    """
    try:
        from .providers.outlook_provider import OutlookEmailProvider
    except Exception:
        from providers.outlook_provider import OutlookEmailProvider
    provider = OutlookEmailProvider()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # Support either provider-specific helper or generic recent-message shape.
            password = ""
            try:
                for row in _read_accounts():
                    if (row.get("email") or "").strip().lower() == email.strip().lower():
                        password = row.get("password", "")
                        break
            except Exception:
                password = ""
            if hasattr(provider, "get_verification_code"):
                code = await provider.get_verification_code(target_email=email, timeout=min(30, int(deadline-time.time())), password=password)
                if code:
                    return code
            elif hasattr(provider, "poll_code"):
                code = await provider.poll_code(email=email, timeout=min(30, int(deadline-time.time())))
                if code:
                    return code
            elif hasattr(provider, "get_latest_code"):
                code = await provider.get_latest_code(email)
                if code:
                    return code
        except Exception as e:
            log_step("outlook_poll_error", email=email, error=str(e)[:200])
        await asyncio.sleep(5)
    return None


def check_worker_health() -> tuple[bool, str]:
    """Compatibility name: verifies that Outlook account source exists."""
    try:
        rows = _read_accounts()
        return bool(rows), f"outlook_accounts={len(rows)}"
    except Exception as e:
        log_step("outlook_health_failed", error=str(e))
        return False, str(e)
