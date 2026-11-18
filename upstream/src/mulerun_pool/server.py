"""Mulerun API Key Pool — single endpoint, load-balances across all keys.

Enhanced with Kiro Gateway patterns:
- Error classification (RECOVERABLE vs FATAL)
- Exponential backoff with probabilistic retry
- Single-key bypass for circuit breaker
- Empty response detection
"""

import asyncio
import json
import os
import time
import logging
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mulerun-pool")

# ── Config ───────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = Path(os.getenv("CREDENTIALS_FILE", str(Path.cwd() / "credentials.json")))
STATE_FILE = Path(os.getenv("STATE_FILE", str(Path.cwd() / "state.json")))

PROXY_API_KEY = os.getenv("PROXY_API_KEY", "change-me")
PROXY_PORT = int(os.getenv("PROXY_PORT", "8089"))
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE_URL", "https://api.mulerun.com").rstrip("/")

# Circuit breaker settings (from Kiro Gateway)
MAX_FAILURES = int(os.getenv("MAX_FAILURES", "3"))
BACKOFF_BASE = int(os.getenv("BACKOFF_BASE", "60"))        # 60s base
BACKOFF_CAP = int(os.getenv("BACKOFF_CAP", "86400"))        # 1 day cap
PROBABILISTIC_RETRY_CHANCE = float(os.getenv("PROBABILISTIC_RETRY_CHANCE", "0.1"))  # 10%

# Empty response detection
EMPTY_RESPONSE_RETRY = os.getenv("EMPTY_RESPONSE_RETRY", "true").lower() in ("true", "1", "yes")


# ── Error Classification (from Kiro Gateway) ─────────────────────────
class ErrorType(Enum):
    """Error classification for circuit breaker decisions."""
    RECOVERABLE = "recoverable"  # Temporary failures (429, 503, timeout, empty response)
    FATAL = "fatal"              # Permanent failures (400, 401, 404)


def classify_error(status_code: int, is_empty: bool = False) -> ErrorType:
    """Classify HTTP error for circuit breaker decisions.
    
    Args:
        status_code: HTTP status code
        is_empty: Whether response content was empty
    
    Returns:
        ErrorType.RECOVERABLE or ErrorType.FATAL
    """
    if is_empty:
        return ErrorType.RECOVERABLE
    
    if status_code in (402, 403, 429, 502, 503, 504):
        return ErrorType.RECOVERABLE
    else:
        return ErrorType.FATAL


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m"
    elif seconds < 86400:
        return f"{int(seconds / 3600)}h"
    else:
        return f"{int(seconds / 86400)}d"


# ── Data Model ───────────────────────────────────────────────────────
@dataclass
class ApiKey:
    key: str
    enabled: bool = True
    failures: int = 0
    last_failure: float = 0.0
    total_requests: int = 0
    total_success: int = 0
    total_errors: int = 0
    avg_latency: float = 0.0

    @property
    def is_available(self) -> bool:
        """Check if key is available (Circuit Breaker logic).
        
        Single-key mode: Always available (bypass circuit breaker)
        Multi-key mode: Exponential backoff with probabilistic retry
        """
        if not self.enabled:
            return False
        if self.failures == 0:
            return True
        
        # Exponential backoff: base * 2^(failures-1), capped
        backoff = min(BACKOFF_BASE * (2 ** (self.failures - 1)), BACKOFF_CAP)
        time_since_failure = time.time() - self.last_failure
        
        if time_since_failure > backoff:
            return True  # Recovery timeout passed
        
        # Probabilistic retry (10% chance, from Kiro Gateway)
        if random.random() < PROBABILISTIC_RETRY_CHANCE:
            return True
        
        return False

    def record_success(self, latency: float):
        """Record successful request (reset failures, update stats)."""
        self.failures = 0
        self.total_requests += 1
        self.total_success += 1
        # Exponential moving average
        self.avg_latency = 0.8 * self.avg_latency + 0.2 * latency if self.avg_latency else latency

    def record_failure(self, error_type: ErrorType):
        """Record failed request (update failures based on error type)."""
        # Only count RECOVERABLE failures for circuit breaker
        if error_type == ErrorType.RECOVERABLE:
            self.failures += 1
            self.last_failure = time.time()
            
            # Calculate backoff for logging
            backoff = min(BACKOFF_BASE * (2 ** (self.failures - 1)), BACKOFF_CAP)
            log.warning(
                f"Key {self.key[:8]}... failure #{self.failures}: "
                f"cooldown={format_duration(backoff)}"
            )
        
        # Always update stats
        self.total_requests += 1
        self.total_errors += 1

    def to_dict(self):
        backoff = min(BACKOFF_BASE * (2 ** (self.failures - 1)), BACKOFF_CAP) if self.failures > 0 else 0
        return {
            "key_hint": self.key[:8] + "..." + self.key[-4:],
            "enabled": self.enabled,
            "available": self.is_available,
            "failures": self.failures,
            "cooldown": format_duration(backoff) if self.failures > 0 else "none",
            "total": self.total_requests,
            "success": self.total_success,
            "errors": self.total_errors,
            "avg_latency_ms": round(self.avg_latency * 1000, 1),
        }


# ── Key Manager ──────────────────────────────────────────────────────
class KeyManager:
    def __init__(self, credentials_file: Path, state_file: Path):
        self.credentials_file = credentials_file
        self.state_file = state_file
        self.keys: list[ApiKey] = []
        self._index = 0  # Sticky round-robin index
        self._dirty = False

    def load_keys(self):
        with open(self.credentials_file) as f:
            creds = json.load(f)
        # Dedupe
        seen = set()
        for entry in creds:
            k = entry["key"]
            if k not in seen:
                seen.add(k)
                self.keys.append(ApiKey(key=k, enabled=entry.get("enabled", True)))
        log.info(f"Loaded {len(self.keys)} API keys")

    def load_state(self):
        if not self.state_file.exists():
            return
        try:
            with open(self.state_file) as f:
                state = json.load(f)
            self._index = state.get("index", 0)
            for s in state.get("keys", []):
                for key in self.keys:
                    if key.key == s["key"]:
                        key.failures = s.get("failures", 0)
                        key.last_failure = s.get("last_failure", 0)
                        key.total_requests = s.get("total", 0)
                        key.total_success = s.get("success", 0)
                        key.total_errors = s.get("errors", 0)
                        key.avg_latency = s.get("avg_latency", 0)
                        break
            log.info("Loaded state")
        except Exception as e:
            log.warning(f"Failed to load state: {e}")

    def save_state(self):
        if not self._dirty:
            return
        state = {
            "index": self._index,
            "keys": [
                {
                    "key": k.key,
                    "failures": k.failures,
                    "last_failure": k.last_failure,
                    "total": k.total_requests,
                    "success": k.total_success,
                    "errors": k.total_errors,
                    "avg_latency": k.avg_latency,
                }
                for k in self.keys
            ],
        }
        tmp = self.state_file.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        tmp.rename(self.state_file)
        self._dirty = False

    def get_next_key(self, exclude: set[str] | None = None) -> Optional[ApiKey]:
        """Get next available key (Circuit Breaker + Sticky + Probabilistic Retry).
        
        Single-key mode: Bypass circuit breaker (from Kiro Gateway)
        Multi-key mode: Sticky round-robin with exponential backoff
        """
        exclude = exclude or set()
        n = len(self.keys)
        if n == 0:
            return None

        # Single-key mode: bypass circuit breaker
        # User should see real API errors instead of generic "Key unavailable"
        if n == 1:
            key = self.keys[0]
            if key.key in exclude or not key.enabled:
                return None
            return key

        # Multi-key mode: iterate with sticky behavior
        for _ in range(n):
            key = self.keys[self._index % n]
            self._index += 1
            if key.key in exclude:
                continue
            if not key.is_available:
                continue
            return key

        # All keys exhausted — try recovering ones (probabilistic)
        for key in self.keys:
            if key.key not in exclude and key.enabled:
                return key
        return None

    def report_success(self, key_str: str, latency: float):
        for k in self.keys:
            if k.key == key_str:
                k.record_success(latency)
                # Update sticky index on success
                try:
                    idx = self.keys.index(k)
                    if self._index % len(self.keys) != idx:
                        self._index = idx
                except ValueError:
                    pass
                self._dirty = True
                break

    def report_failure(self, key_str: str, error_type: ErrorType):
        for k in self.keys:
            if k.key == key_str:
                k.record_failure(error_type)
                self._dirty = True
                break

    def get_stats(self) -> dict:
        available = sum(1 for k in self.keys if k.is_available)
        return {
            "total_keys": len(self.keys),
            "available_keys": available,
            "disabled_keys": sum(1 for k in self.keys if not k.enabled),
            "single_key_mode": len(self.keys) == 1,
            "keys": [k.to_dict() for k in self.keys],
        }


# ── Globals ──────────────────────────────────────────────────────────
manager: KeyManager = None
http_client: httpx.AsyncClient = None


# ── App ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global manager, http_client

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        http2=False,
    )

    manager = KeyManager(CREDENTIALS_FILE, STATE_FILE)
    manager.load_keys()
    manager.load_state()

    log.info(f"Mulerun Pool started — {len(manager.keys)} keys, port {PROXY_PORT}")
    if len(manager.keys) == 1:
        log.info("Single-key mode: circuit breaker disabled")

    # Background state saver
    async def save_loop():
        while True:
            await asyncio.sleep(10)
            manager.save_state()

    task = asyncio.create_task(save_loop())
    yield
    task.cancel()
    manager.save_state()
    await http_client.aclose()
    log.info("Shutdown complete")


app = FastAPI(title="Mulerun Key Pool", lifespan=lifespan)


# ── Auth Middleware ───────────────────────────────────────────────────
def check_auth(request: Request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != PROXY_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Response Validation ──────────────────────────────────────────────
def is_empty_response(data: dict) -> bool:
    """Check if response is empty/useless.
    
    Detects:
    - No choices
    - Empty content
    - Zero completion tokens
    - Truncated responses (finish_reason=length with tiny content)
    """
    choices = data.get("choices", [])
    if not choices:
        return True
    
    for c in choices:
        msg = c.get("message", {})
        content = msg.get("content", "")
        if not content or not content.strip():
            return True
        # Check for truncation with minimal content
        if c.get("finish_reason") == "length" and len(content) < 10:
            return True
    
    usage = data.get("usage", {})
    completion_tokens = usage.get("completion_tokens", -1)
    if completion_tokens == 0:
        return True
    
    return False


# ── Routes ───────────────────────────────────────────────────────────
@app.get("/")
async def health():
    stats = manager.get_stats()
    return {"status": "ok", "available_keys": stats["available_keys"], "total_keys": stats["total_keys"]}


@app.get("/health")
async def health_detail():
    return manager.get_stats()


@app.get("/v1/models")
async def list_models(request: Request):
    check_auth(request)
    return {"object": "list", "data": [{"id": "claude-opus-4-6", "object": "model", "owned_by": "mulerun"}]}


@app.post("/v1/chat/completions")
@app.post("/v1/messages")
async def proxy_request(request: Request):
    check_auth(request)

    body = await request.body()
    max_attempts = len(manager.keys) * 2
    tried: set[str] = set()

    for attempt in range(max_attempts):
        key = manager.get_next_key(exclude=tried)
        if key is None:
            return JSONResponse(
                status_code=503,
                content={"error": {"message": "All keys unavailable", "type": "pool_error"}},
            )
        tried.add(key.key)

        try:
            t0 = time.time()
            resp = await http_client.request(
                method="POST",
                url=f"{UPSTREAM_BASE}/v1/chat/completions",
                headers={
                    "Content-Type": request.headers.get("content-type", "application/json"),
                    "Authorization": f"Bearer {key.key}",
                },
                content=body,
            )
            latency = time.time() - t0

            if resp.status_code == 200:
                # Streaming detection — pass through without content check
                ct = resp.headers.get("content-type", "")
                if "text/event-stream" in ct:
                    manager.report_success(key.key, latency)
                    async def stream_iter():
                        async for chunk in resp.aiter_bytes(1024):
                            yield chunk
                    return StreamingResponse(stream_iter(), media_type="text/event-stream")

                # Non-streaming: validate response content
                try:
                    data = resp.json()
                except Exception:
                    error_type = ErrorType.RECOVERABLE
                    manager.report_failure(key.key, error_type)
                    log.warning(f"Key {key.key[:8]}... invalid JSON in 200 response, trying next")
                    continue

                # Check for empty/useless responses
                if EMPTY_RESPONSE_RETRY and is_empty_response(data):
                    usage = data.get("usage", {})
                    completion_tokens = usage.get("completion_tokens", -1)
                    error_type = ErrorType.RECOVERABLE
                    manager.report_failure(key.key, error_type)
                    log.warning(
                        f"Key {key.key[:8]}... empty response "
                        f"(completion_tokens={completion_tokens}), "
                        f"trying next (attempt {attempt+1})"
                    )
                    continue

                manager.report_success(key.key, latency)
                return JSONResponse(content=data, status_code=200)

            # 4xx/5xx — classify and handle
            error_type = classify_error(resp.status_code)
            
            if error_type == ErrorType.RECOVERABLE:
                # Recoverable — try next key
                manager.report_failure(key.key, error_type)
                log.warning(
                    f"Key {key.key[:8]}... got {resp.status_code} "
                    f"({error_type.value}), trying next (attempt {attempt+1})"
                )
                continue
            else:
                # Fatal — return to client
                try:
                    err = resp.json()
                except:
                    err = {"error": {"message": resp.text[:200], "status_code": resp.status_code}}
                return JSONResponse(content=err, status_code=resp.status_code)

        except httpx.TimeoutException:
            error_type = ErrorType.RECOVERABLE
            manager.report_failure(key.key, error_type)
            log.warning(f"Key {key.key[:8]}... timeout, trying next (attempt {attempt+1})")
            continue
        except httpx.RequestError as e:
            error_type = ErrorType.RECOVERABLE
            manager.report_failure(key.key, error_type)
            log.warning(f"Key {key.key[:8]}... network error: {e}, trying next (attempt {attempt+1})")
            continue

    return JSONResponse(
        status_code=503,
        content={"error": {"message": "All keys exhausted after retries", "type": "pool_error"}},
    )


# ── Admin Routes ─────────────────────────────────────────────────────
@app.get("/admin/stats")
async def admin_stats(request: Request):
    check_auth(request)
    return manager.get_stats()


@app.post("/admin/keys")
async def add_key(request: Request):
    check_auth(request)
    body = await request.json()
    new_key = body.get("key")
    if not new_key:
        raise HTTPException(400, "Missing 'key'")
    for k in manager.keys:
        if k.key == new_key:
            raise HTTPException(409, "Key already exists")
    api_key = ApiKey(key=new_key)
    manager.keys.append(api_key)
    # Persist to credentials.json
    creds = [{"key": k.key, "enabled": k.enabled} for k in manager.keys]
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    manager._dirty = True
    log.info(f"Added key {new_key[:8]}...")
    return {"status": "added", "total_keys": len(manager.keys)}


@app.delete("/admin/keys/{key_id}")
async def remove_key(key_id: int, request: Request):
    check_auth(request)
    if key_id < 0 or key_id >= len(manager.keys):
        raise HTTPException(404, "Key index out of range")
    removed = manager.keys.pop(key_id)
    creds = [{"key": k.key, "enabled": k.enabled} for k in manager.keys]
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    manager._dirty = True
    log.info(f"Removed key {removed.key[:8]}...")
    return {"status": "removed", "total_keys": len(manager.keys)}


@app.post("/admin/keys/{key_id}/enable")
async def enable_key(key_id: int, request: Request):
    check_auth(request)
    if key_id < 0 or key_id >= len(manager.keys):
        raise HTTPException(404, "Key index out of range")
    body = await request.json()
    manager.keys[key_id].enabled = body.get("enabled", True)
    manager.keys[key_id].failures = 0  # Reset circuit breaker
    creds = [{"key": k.key, "enabled": k.enabled} for k in manager.keys]
    with open(CREDENTIALS_FILE, "w") as f:
        json.dump(creds, f, indent=2)
    manager._dirty = True
    return {"status": "updated", "enabled": manager.keys[key_id].enabled}


@app.post("/admin/reset")
async def reset_circuit_breakers(request: Request):
    check_auth(request)
    for k in manager.keys:
        k.failures = 0
        k.enabled = True
    manager._dirty = True
    return {"status": "all reset", "total_keys": len(manager.keys)}


# ── Main ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run("mulerun_pool.server:app", host="0.0.0.0", port=PROXY_PORT, log_level="info")
