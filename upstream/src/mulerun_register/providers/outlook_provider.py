"""Outlook/Hotmail verification-code polling.

Default mode uses IMAP credentials from the reserved Outlook account row. Optional
Graph mode can be enabled with OUTLOOK_GRAPH_TOKEN for operators that already
manage Microsoft OAuth externally.
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import os
import re
import time
from email.header import decode_header, make_header
from typing import Optional

import aiohttp

CODE_PATTERNS = [
    re.compile(r"(?:code|verification|verify|confirm|验证码|确认)[^0-9]{0,30}([0-9]{4,8})", re.I),
    re.compile(r"\b([0-9]{6})\b"),
    re.compile(r"\b([0-9]{4,8})\b"),
]


def _decode_mime(value: str) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:
        return value or ""


def _extract_text(msg: email.message.Message) -> str:
    parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                parts.append(payload.decode(charset, errors="replace"))
            except Exception:
                parts.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        parts.append(payload.decode(charset, errors="replace"))
    return re.sub(r"<[^>]+>", " ", "\n".join(parts))


def extract_code(text: str) -> Optional[str]:
    for pat in CODE_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(1)
    return None


class OutlookEmailProvider:
    """Poll a target Outlook/Hotmail mailbox for MuleRun verification codes."""

    def __init__(self, password: str = "", imap_host: str | None = None, graph_token: str | None = None):
        self.password = password or os.getenv("OUTLOOK_PASSWORD", "")
        self.imap_host = imap_host or os.getenv("OUTLOOK_IMAP_HOST", "outlook.office365.com")
        self.graph_token = graph_token or os.getenv("OUTLOOK_GRAPH_TOKEN", "")
        self.graph_url = os.getenv("OUTLOOK_GRAPH_URL", "https://graph.microsoft.com/v1.0")

    async def get_verification_code(self, target_email: str, timeout: int = 60, password: str = "") -> Optional[str]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = None
            if self.graph_token:
                code = await self._poll_graph_once(target_email)
            else:
                code = await asyncio.to_thread(self._poll_imap_once, target_email, password or self.password)
            if code:
                print(f" ✅ Outlook code received for {target_email}")
                return code
            await asyncio.sleep(5)
        return None

    def _poll_imap_once(self, target_email: str, password: str) -> Optional[str]:
        if not password:
            return None
        with imaplib.IMAP4_SSL(self.imap_host) as imap:
            imap.login(target_email, password)
            imap.select("INBOX")
            typ, data = imap.search(None, "ALL")
            if typ != "OK" or not data or not data[0]:
                return None
            ids = data[0].split()[-20:]
            for mid in reversed(ids):
                typ, fetched = imap.fetch(mid, "(RFC822)")
                if typ != "OK" or not fetched:
                    continue
                raw = fetched[0][1]
                msg = email.message_from_bytes(raw)
                subject = _decode_mime(msg.get("Subject", ""))
                sender = _decode_mime(msg.get("From", ""))
                body = _extract_text(msg)
                haystack = f"{subject}\n{sender}\n{body}"
                if "mulerun" not in haystack.lower() and "verification" not in haystack.lower() and "code" not in haystack.lower():
                    continue
                code = extract_code(haystack)
                if code:
                    return code
        return None

    async def _poll_graph_once(self, target_email: str) -> Optional[str]:
        headers = {"Authorization": f"Bearer {self.graph_token}"}
        url = f"{self.graph_url}/me/mailfolders/inbox/messages?$top=20&$orderby=receivedDateTime desc"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            for msg in data.get("value", []):
                subject = msg.get("subject", "")
                to_list = msg.get("toRecipients", []) or []
                if to_list and target_email.lower() not in str(to_list).lower():
                    continue
                body_preview = msg.get("bodyPreview", "")
                code = extract_code(f"{subject}\n{body_preview}")
                if code:
                    return code
        return None
