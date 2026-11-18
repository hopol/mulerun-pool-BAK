"""MuleRun v6 - 全局配置"""

import json
import os
import random
import shutil
import string
import tempfile
import time
import uuid as _uuid

# ── 邀请/服务 ────────────────────────────────────────────────────
INVITE_URL = os.getenv("MR_INVITE_URL", "https://mulerun.com/invitation/YOUR_INVITE_CODE")
OUTLOOK_ACCOUNTS = os.getenv("MR_OUTLOOK_ACCOUNTS", "accounts.outlook.csv")

# ── 注册参数 ─────────────────────────────────────────────────────
PASSWORD = os.getenv("MR_DEFAULT_PASSWORD", "change-me")
MAX_TOKEN_ATTEMPTS = 3
VERIFICATION_TIMEOUT = int(os.getenv("MR_VERIFICATION_TIMEOUT", "60"))
RESULT_FILE = "/tmp/mulerun-results.json"
MAX_CONCURRENCY = int(os.getenv("MR_CONCURRENCY", "2"))

# ── TLS Fingerprint 伪装 ─────────────────────────────────────────
# curl_cffi 模拟浏览器 TLS 指纹，用于 HTTP API 请求
TLS_IMPERSONATE = os.getenv("MR_TLS_IMPERSONATE", "chrome124").strip()  # chrome110-131, edge, safari
USE_CURL_CFFI = os.getenv("MR_USE_CURL_CFFI", "1").strip().lower() in {"1", "true", "yes", "on"}

# ── 代理池（2026-04-17 mihomo 负载均衡）────────────────────────────
# 使用 mihomo (Clash Meta) 的 round-robin 负载均衡
# 28 个节点自动轮换，每次请求不同出口 IP
PROXY_POOL = []  # 禁用代理池，使用直连模式
PROXY_ROTATION_MODE = os.getenv("MR_PROXY_ROTATION", "random").strip()  # sequential, random, failover
PROXY_HEALTHCHECK_URL = os.getenv("MR_PROXY_HC_URL", "https://api.ipify.org").strip()
PROXY_HEALTHCHECK_TIMEOUT = int(os.getenv("MR_PROXY_HC_TIMEOUT", "4"))

# ── 浏览器 ───────────────────────────────────────────────────────
DEFAULT_CHROME_PATH = "/root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
DEFAULT_PROXY_SERVER = os.getenv("MR_PROXY_SERVER", PROXY_POOL[0] if PROXY_POOL else "").strip()
PROXY_SERVER = os.getenv("MR_PROXY_SERVER", DEFAULT_PROXY_SERVER).strip()
USER_DATA_DIR = os.getenv("MR_USER_DATA_DIR", "").strip()
FRESH_PROFILE = os.getenv("MR_FRESH_PROFILE", "1").strip().lower() in {"1", "true", "yes", "on"}
PROFILE_ROOT = os.getenv("MR_PROFILE_ROOT", "/tmp/mr-browser-profiles").strip()
EXTRA_BROWSER_ARGS_RAW = os.getenv("MR_EXTRA_BROWSER_ARGS", "").strip()
LANG_OVERRIDE = os.getenv("MR_BROWSER_LANG", "").strip()  # 空时随机化


def env_flag(name, default=None):
    raw = os.getenv(name, "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def resolve_headless_mode():
    forced = env_flag("MR_HEADLESS", None)
    if forced is not None:
        return forced
    # 没 DISPLAY 时优先自动切 headless，避免强依赖 xvfb
    return not bool(os.getenv("DISPLAY"))


HEADLESS = resolve_headless_mode()

# ── 浏览器指纹随机化 ─────────────────────────────────────────────
TIMEZONE_OVERRIDE = os.getenv("MR_TIMEZONE_OVERRIDE", "").strip()  # 空时随机化，如 "Asia/Shanghai", "America/New_York"
RANDOMIZE_FINGERPRINT = os.getenv("MR_RANDOMIZE_FINGERPRINT", "1").strip().lower() in {"1", "true", "yes", "on"}

# 常用语言列表（用于随机化）
LANG_POOL = ["en-US", "en-GB", "en-CA", "en-AU", "zh-CN", "zh-TW", "ja-JP", "ko-KR", "es-ES", "fr-FR"]
# 常用时区列表（用于随机化）
TIMEZONE_POOL = [
    "America/New_York", "America/Los_Angeles", "America/Chicago", "America/Denver",
    "Europe/London", "Europe/Paris", "Europe/Berlin", "Europe/Amsterdam",
    "Asia/Tokyo", "Asia/Shanghai", "Asia/Hong_Kong", "Asia/Singapore", "Asia/Seoul",
    "Australia/Sydney", "Pacific/Auckland"
]

# ── 行为 profile（加大伪装 2026-04-17）────────────────────────────
WARMUP_PROFILE = os.getenv("MR_WARMUP_PROFILE", "invite_read_hover_focus")
TRACE_PROFILE = os.getenv("MR_TRACE_PROFILE", "deep_focus")
RHYTHM_PROFILE = os.getenv("MR_RHYTHM_PROFILE", "baseline")
TYPING_MODE = os.getenv("MR_TYPING_MODE", "charwise")

# ── 时间窗口（加大行为伪装 2026-04-17）────────────────────────────
INVITE_SETTLE_SECONDS = float(os.getenv("MR_INVITE_SETTLE_SECONDS", "12"))
FORM_SETTLE_SECONDS = float(os.getenv("MR_FORM_SETTLE_SECONDS", "5"))
INTER_FIELD_GAP_SECONDS = float(os.getenv("MR_INTER_FIELD_GAP_SECONDS", "1.5"))
POST_FIELD_SETTLE_SECONDS = float(os.getenv("MR_POST_FIELD_SETTLE_SECONDS", "2.0"))
TURNSTILE_OBSERVE_SECONDS = float(os.getenv("MR_TURNSTILE_OBSERVE_SECONDS", "25"))  # CDN 节点需要更长加载
TURNSTILE_NUDGE_SECONDS = float(os.getenv("MR_TURNSTILE_NUDGE_SECONDS", "12"))
TURNSTILE_FINAL_SECONDS = float(os.getenv("MR_TURNSTILE_FINAL_SECONDS", "10"))
POST_CONTINUE_WINDOW_SECONDS = int(os.getenv("MR_POST_CONTINUE_WINDOW_SECONDS", "15"))
PRE_SUBMIT_OBSERVE_SECONDS = float(os.getenv("MR_PRE_SUBMIT_OBSERVE_SECONDS", "3.0"))

# ── Post-Continue Trace ──────────────────────────────────────────
POST_CONTINUE_TRACE = os.getenv("MR_POST_CONTINUE_TRACE", "0").strip().lower() in {"1", "true", "yes", "on"}
POST_CONTINUE_TRACE_FILE = os.getenv("MR_POST_CONTINUE_TRACE_FILE", "/tmp/mr_post_continue_trace.jsonl").strip() or "/tmp/mr_post_continue_trace.jsonl"
POST_CONTINUE_TRACE_LABEL = os.getenv("MR_POST_CONTINUE_TRACE_LABEL", "").strip()
MATRIX_TAG = os.getenv("MR_MATRIX_TAG", "")

# ── 运行级标识 ───────────────────────────────────────────────────
RUN_ID = os.getenv("MR_RUN_ID", f"{int(time.time())}-{_uuid.uuid4().hex[:6]}")
SCREENSHOT_DIR = f"/tmp/mr-screenshots/{RUN_ID}"
LOG_FILE = f"/tmp/mr-logs/{RUN_ID}.jsonl"
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


# ── 工具函数 ─────────────────────────────────────────────────────

def random_local_email():
    prefix = ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    return prefix, prefix  # domain-email generation removed; use Outlook account file


def compute_user_data_dir():
    if USER_DATA_DIR:
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        return USER_DATA_DIR
    if FRESH_PROFILE:
        os.makedirs(PROFILE_ROOT, exist_ok=True)
        return tempfile.mkdtemp(prefix="mr-profile-", dir=PROFILE_ROOT)
    return None


def parse_extra_browser_args():
    if not EXTRA_BROWSER_ARGS_RAW:
        return []
    raw = EXTRA_BROWSER_ARGS_RAW
    if raw.startswith('['):
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(x) for x in data if str(x).strip()]
        except Exception:
            pass
    return [part.strip() for part in raw.split(';;') if part.strip()]


def build_browser_args(proxy_override=None):
    """构建浏览器参数，支持代理覆盖"""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--window-size=1280,900",
        "--no-sandbox",
        "--disable-setuid-sandbox",
    ]
    # 优先使用传入的代理，其次使用全局配置
    proxy = proxy_override or PROXY_SERVER
    if proxy:
        args.append(f"--proxy-server={proxy}")
    args.extend(parse_extra_browser_args())
    return args


def resolve_browser_path():
    candidates = []
    env_path = os.getenv("MR_CHROME_PATH", "").strip()
    if env_path:
        candidates.append(env_path)
    candidates.append(DEFAULT_CHROME_PATH)

    # 自动发现最新 Playwright Chromium 缓存，避免目录版本写死
    pw_root = os.path.expanduser("~/.cache/ms-playwright")
    if os.path.isdir(pw_root):
        for name in sorted(os.listdir(pw_root), reverse=True):
            if not name.startswith("chromium-"):
                continue
            candidates.append(os.path.join(pw_root, name, "chrome-linux64", "chrome"))

    for binary in ("chromium", "google-chrome", "chromium-browser", "chrome"):
        found = shutil.which(binary)
        if found:
            candidates.append(found)
    snap_chromium = "/snap/bin/chromium"
    if os.path.exists(snap_chromium):
        candidates.append(snap_chromium)
    seen = set()
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.exists(path):
            return path
    raise FileNotFoundError(
        "No browser executable found. Tried MR_CHROME_PATH, Playwright cache, and system chromium/google-chrome paths."
    )


# ── TLS Fingerprint 伪装 ─────────────────────────────────────────
def get_tls_session():
    """返回 curl_cffi 会话（模拟浏览器 TLS 指纹）"""
    if not USE_CURL_CFFI:
        return None
    try:
        from curl_cffi import requests as curl_requests
        # 创建持久会话，模拟真实浏览器 TLS 指纹
        session = curl_requests.Session(impersonate=TLS_IMPERSONATE)
        return session
    except Exception as e:
        print(f"    ⚠️ curl_cffi 初始化失败: {e}")
        return None


# ── 浏览器指纹随机化 ─────────────────────────────────────────────
def randomize_browser_fingerprint():
    """随机生成浏览器指纹参数（语言、时区等）"""
    if not RANDOMIZE_FINGERPRINT:
        # 返回配置值或默认值
        return LANG_OVERRIDE or "en-US", TIMEZONE_OVERRIDE or "UTC"

    # 随机选择语言和时区
    lang = LANG_OVERRIDE or random.choice(LANG_POOL)
    tz = TIMEZONE_OVERRIDE or random.choice(TIMEZONE_POOL)
    return lang, tz


def build_fingerprint_args():
    """生成用于浏览器的指纹参数"""
    lang, tz = randomize_browser_fingerprint()

    # 构建 nodriver 启动参数
    args = [
        f"--lang={lang}",
        f"--timezone={tz}",
        f"--timezone-zone={tz}",
    ]

    # 随机化屏幕分辨率（常见组合）
    resolutions = [(1920, 1080), (1366, 768), (1440, 900), (1536, 864), (2560, 1440)]
    w, h = random.choice(resolutions)
    args.append(f"--window-size={w},{h}")

    # 随机化 user-agent（已禁用：Chrome 124 UA + Chrome 145 实际版本不匹配，Turnstile 会拒绝）
    # user_agents = [
    #     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    #     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    #     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # ]
    # args.append(f"--user-agent={random.choice(user_agents)}")

    return args, lang, tz


CHROME_PATH = resolve_browser_path()


def normalize_timeout(value, minimum=0):
    try:
        num = int(round(float(value)))
    except Exception:
        num = minimum
    if num < minimum:
        num = minimum
    return num


# ── 代理池轮询工具函数 ───────────────────────────────────────────
_proxy_index = 0

def get_next_proxy(rotation_mode=None):
    """获取下一个代理，支持四种模式：sequential, random, failover, failover-strict"""
    global _proxy_index
    mode = rotation_mode or PROXY_ROTATION_MODE

    if mode == "random":
        return random.choice(PROXY_POOL)
    elif mode in ("failover", "failover-strict"):
        # failover: 返回当前 index 的代理（调用方负责验活后降级）
        # failover-strict: 预先过滤掉已知不可用的
        proxy = PROXY_POOL[_proxy_index % len(PROXY_POOL)]
        _proxy_index += 1
        return proxy
    else:  # sequential
        proxy = PROXY_POOL[_proxy_index % len(PROXY_POOL)]
        _proxy_index += 1
        return proxy


def check_proxy_health(proxy_url):
    """快速检查单个代理是否可达，返回 (ok, exit_ip|error)"""
    import subprocess
    try:
        r = subprocess.run(
            ["curl", "-sx", f"socks5h://{proxy_url.replace('socks5://','').replace('socks5h://','')}",
             "--max-time", str(PROXY_HEALTHCHECK_TIMEOUT), "-s", PROXY_HEALTHCHECK_URL],
            capture_output=True, text=True, timeout=PROXY_HEALTHCHECK_TIMEOUT + 2
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip()
        return False, r.stderr[:80] if r.stderr else f"curl exit {r.returncode}"
    except Exception as e:
        return False, str(e)[:80]


def get_healthy_proxy(max_attempts=None):
    """轮询代理池，返回第一个健康代理（含 exit_ip）。全部失败返回 None（直连模式）。"""
    if not PROXY_POOL:
        return None  # 无代理池，直连模式
    attempts = max_attempts or len(PROXY_POOL)
    tried = 0
    while tried < attempts:
        proxy = get_next_proxy()
        tried += 1
        ok, detail = check_proxy_health(proxy)
        if ok:
            print(f"    ✅ 代理健康: {proxy} → {detail}")
            return proxy
        print(f"    ⚠️ 代理不可达: {proxy} → {detail}，尝试下一个...")
    raise RuntimeError(f"所有代理均不可达（已尝试 {tried} 个）")

def reset_proxy_index():
    """重置代理轮询索引"""
    global _proxy_index
    _proxy_index = 0
