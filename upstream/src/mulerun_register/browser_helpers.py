"""MuleRun v6 - 浏览器/页面操作工具集"""

import asyncio
import json
import random
import time
import nodriver as uc

from .config import (
    SCREENSHOT_DIR,
    PASSWORD,
    TYPING_MODE,
    WARMUP_PROFILE,
    TRACE_PROFILE,
    POST_CONTINUE_TRACE,
    POST_CONTINUE_TRACE_FILE,
    POST_CONTINUE_TRACE_LABEL,
    MATRIX_TAG,
    normalize_timeout,
)


def ss_path(tag):
    """生成隔离的截图路径，避免并行覆盖"""
    return f"{SCREENSHOT_DIR}/{tag}.png"


async def dump_page(page, tag=""):
    info = await page.evaluate(
        """(function(){
        var r = {url: location.href, inputs:[], buttons:[], text: (document.body.innerText||'').substring(0,1200)};
        document.querySelectorAll('input').forEach(function(i){
            var vis = i.offsetParent!==null;
            r.inputs.push({
              name:i.name||'',type:i.type||'',ph:i.placeholder||'',val:(i.value||'').substring(0,40),
              vis:vis, checked:!!i.checked, aria:i.getAttribute('aria-invalid')
            });
        });
        document.querySelectorAll('button').forEach(function(b){
            if(b.offsetParent!==null)
                r.buttons.push({text:(b.innerText||'').trim().substring(0,60),disabled:!!b.disabled});
        });
        var t = document.querySelector('input[name="cf-turnstile-response"]');
        r.turnstile = t ? (t.value||'') : '';
        return JSON.stringify(r);
    })()"""
    )

    if isinstance(info, tuple):
        info = info[0]
    if isinstance(info, (bytes, bytearray)):
        info = info.decode("utf-8", errors="ignore")

    if not isinstance(info, str):
        type_name = type(info).__name__
        print(f"  [{tag}] dump_page evaluate returned non-string: {type_name}")
        return {
            "url": "", "inputs": [], "buttons": [], "text": "", "turnstile": "",
            "_error": f"evaluate_non_string:{type_name}",
        }

    try:
        d = json.loads(info)
    except Exception as e:
        preview = info[:200].replace("\n", " ")
        print(f"  [{tag}] dump_page json parse failed: {e}; preview={preview}")
        return {
            "url": "", "inputs": [], "buttons": [], "text": "", "turnstile": "",
            "_error": f"json_parse_failed:{e}",
        }

    print(f"  [{tag}] URL={d['url']}")
    print(f"  [{tag}] {len(d['inputs'])} inputs, {len(d['buttons'])} buttons, tokenLen={len(d.get('turnstile',''))}")
    for i in d["inputs"]:
        if i["vis"] or i["type"] == "hidden" and "turnstile" in i["name"]:
            extra = f" checked={i['checked']}" if i["type"] == "checkbox" else ""
            print(f"    {'👁' if i['vis'] else '🔒'} {i['name']}({i['type']}) ph={i['ph']} val={i['val']} aria={i['aria']}{extra}")
    for b in d["buttons"]:
        print(f"    {'✅' if not b['disabled'] else '⬜'} {b['text']}")
    return d


async def wait_visible_button(page, keywords, timeout=15):
    kws = [k.lower() for k in keywords]
    for _ in range(timeout):
        text = await page.evaluate(
            f"""(function(){{
            var kws = {json.dumps(kws)};
            for (var b of document.querySelectorAll('button')) {{
              var t = (b.innerText||'').trim().toLowerCase();
              if (b.offsetParent!==null) {{
                for (var k of kws) if (t.includes(k)) return (b.innerText||'').trim();
              }}
            }}
            return null;
        }})()"""
        )
        if text:
            return text
        await asyncio.sleep(1)
    return None


async def click_btn(page, keywords, timeout=15, allow_disabled=False):
    kws = [k.lower() for k in keywords]
    for _ in range(timeout):
        r = await page.evaluate(
            f"""(function(){{
            var kws = {json.dumps(kws)};
            for (var b of document.querySelectorAll('button')) {{
                var t = (b.innerText||'').trim().toLowerCase();
                if (b.offsetParent!==null && ({'true' if allow_disabled else '!b.disabled'}) && t.length < 80) {{
                    for (var k of kws) if (t.includes(k)) {{ b.click(); return {{text:(b.innerText||'').trim(), disabled:!!b.disabled}}; }}
                }}
            }}
            return null;
        }})()"""
        )
        if r:
            return r
        await asyncio.sleep(1)
    return None


async def wait_turnstile(page, timeout=30):
    """等待并尝试触发 Turnstile token - 改进版(2026-04-01):
    主动调用 turnstile.execute() 而非只检查值，解决 token 延迟生成问题。
    """
    timeout = normalize_timeout(timeout, minimum=0)

    async def try_trigger_turnstile():
        """尝试触发 Turnstile - 多策略"""
        try:
            return await page.evaluate(
                """(function(){
                // 策略1: turnstile API 存在时主动 execute
                if (typeof window.turnstile !== 'undefined') {
                    try {
                        var containers = document.querySelectorAll('[data-cdata],.cf-turnstile,[data-sitekey]');
                        for (var c of containers) {
                            try { window.turnstile.execute(c); return 'execute-container'; } catch(e) {}
                        }
                    } catch(e) {}
                    try { window.turnstile.execute(); return 'execute-global'; } catch(e) {}
                }
                // 策略2: 点击 iframe
                var iframes = document.querySelectorAll('iframe');
                for (var f of iframes) {
                    if ((f.src||'').includes('turnstile') || (f.src||'').includes('challenges')) {
                        f.click(); return 'clicked-iframe';
                    }
                }
                // 策略3: focus input
                var input = document.querySelector('input[name="cf-turnstile-response"]');
                if (input) { input.focus(); return 'focused-input'; }
                return 'not-found';
            })()""")
        except Exception as e:
            return f'error:{e}'

    for i in range(timeout):
        if i % 3 == 0:
            await try_trigger_turnstile()
        token = await page.evaluate(
            """(function(){
            var el = document.querySelector('input[name="cf-turnstile-response"]');
            return el ? el.value : null;
        })()"""
        )
        if token and len(str(token)) > 20:
            return token
        await asyncio.sleep(1)
    return None


async def body_nudge(page):
    try:
        return await page.evaluate(
            """(function(){
            if (!document.body) return 'no-body';
            document.body.click();
            return 'body-clicked';
        })()"""
        )
    except Exception:
        return 'body-click-failed'


async def micro_scroll(page, down=360, back=True):
    try:
        await page.evaluate(
            f"""(function(){{
            window.scrollBy({{top:{int(down)}, behavior:'instant'}});
            return 'scrolled';
        }})()"""
        )
        await asyncio.sleep(0.4)
        if back:
            await page.evaluate(
                f"""(function(){{
                window.scrollBy({{top:-{int(down)}, behavior:'instant'}});
                return 'scrolled-back';
            }})()"""
            )
            await asyncio.sleep(0.4)
        return 'scroll-ok'
    except Exception:
        return 'scroll-failed'


async def hover_button(page, keywords):
    kws = [k.lower() for k in keywords]
    try:
        return await page.evaluate(
            f"""(function(){{
            var kws = {json.dumps(kws)};
            for (var b of document.querySelectorAll('button')) {{
                var t = (b.innerText || '').trim().toLowerCase();
                if (b.offsetParent !== null) {{
                    for (var k of kws) if (t.includes(k)) {{
                        ['mousemove','mouseover','mouseenter'].forEach(function(evt){{
                            b.dispatchEvent(new MouseEvent(evt, {{bubbles:true, cancelable:true, view:window}}));
                        }});
                        return (b.innerText || '').trim();
                    }}
                }}
            }}
            return null;
        }})()"""
        )
    except Exception:
        return None


async def focus_shuffle(page):
    try:
        return await page.evaluate(
            """(function(){
            try { if (document.activeElement) document.activeElement.blur(); } catch (e) {}
            if (document.body) document.body.focus && document.body.focus();
            document.dispatchEvent(new MouseEvent('mousemove', {bubbles:true, cancelable:true, view:window, clientX:240, clientY:180}));
            return 'focus-shuffled';
        })()"""
        )
    except Exception:
        return 'focus-shuffle-failed'


async def apply_warmup_profile(page):
    print(f"    warmup={WARMUP_PROFILE or 'baseline'}")
    if WARMUP_PROFILE == 'baseline':
        return 'baseline'
    if WARMUP_PROFILE == 'invite_scroll':
        await micro_scroll(page, down=420, back=True)
        await asyncio.sleep(0.8)
        return 'invite_scroll'
    if WARMUP_PROFILE == 'invite_hover_focus':
        await micro_scroll(page, down=420, back=True)
        await hover_button(page, ['sign up with email'])
        await asyncio.sleep(0.6)
        await body_nudge(page)
        await asyncio.sleep(0.8)
        return 'invite_hover_focus'
    if WARMUP_PROFILE == 'invite_breathe':
        await micro_scroll(page, down=520, back=True)
        await asyncio.sleep(1.2)
        await body_nudge(page)
        await asyncio.sleep(1.0)
        return 'invite_breathe'
    if WARMUP_PROFILE == 'invite_deep_read':
        await micro_scroll(page, down=420, back=False)
        await asyncio.sleep(1.4)
        await micro_scroll(page, down=220, back=True)
        await asyncio.sleep(1.0)
        await hover_button(page, ['sign up with email'])
        await asyncio.sleep(0.8)
        await focus_shuffle(page)
        await asyncio.sleep(0.8)
        return 'invite_deep_read'
    if WARMUP_PROFILE == 'invite_scroll_linger':
        await micro_scroll(page, down=640, back=False)
        await asyncio.sleep(1.6)
        await micro_scroll(page, down=280, back=True)
        await asyncio.sleep(1.2)
        await body_nudge(page)
        await asyncio.sleep(0.8)
        return 'invite_scroll_linger'
    if WARMUP_PROFILE == 'invite_read_hover_focus':
        await micro_scroll(page, down=360, back=True)
        await asyncio.sleep(1.0)
        await hover_button(page, ['google', 'apple', 'sign up with email'])
        await asyncio.sleep(0.8)
        await focus_shuffle(page)
        await asyncio.sleep(0.6)
        await body_nudge(page)
        await asyncio.sleep(0.8)
        return 'invite_read_hover_focus'
    return f'unknown:{WARMUP_PROFILE}'


async def apply_trace_profile(page, stage):
    if TRACE_PROFILE == 'baseline':
        return 'baseline'
    if TRACE_PROFILE == 'body_touch':
        await body_nudge(page)
        await asyncio.sleep(0.35)
        return f'body_touch:{stage}'
    if TRACE_PROFILE == 'focus_shuffle':
        await focus_shuffle(page)
        await asyncio.sleep(0.35)
        return f'focus_shuffle:{stage}'
    if TRACE_PROFILE == 'scroll_focus':
        await micro_scroll(page, down=140, back=True)
        await focus_shuffle(page)
        await asyncio.sleep(0.35)
        return f'scroll_focus:{stage}'
    if TRACE_PROFILE == 'hover_continue':
        if 'continue' in stage:
            await hover_button(page, ['continue'])
            await asyncio.sleep(0.45)
        else:
            await body_nudge(page)
            await asyncio.sleep(0.25)
        return f'hover_continue:{stage}'
    if TRACE_PROFILE == 'deep_focus':
        await focus_shuffle(page)
        await asyncio.sleep(0.35)
        await body_nudge(page)
        await asyncio.sleep(0.35)
        return f'deep_focus:{stage}'
    if TRACE_PROFILE == 'reading_touch':
        if 'continue' in stage or 'signup' in stage:
            await hover_button(page, ['sign up with email', 'continue'])
            await asyncio.sleep(0.5)
        await micro_scroll(page, down=120, back=True)
        await asyncio.sleep(0.4)
        return f'reading_touch:{stage}'
    return f'unknown:{TRACE_PROFILE}:{stage}'


async def wait_turnstile_ready(page, observe_seconds=8, nudge_after=10, final_timeout=5):
    start = time.time()
    last_continue_enabled = False

    while time.time() - start < observe_seconds:
        token = await wait_turnstile(page, timeout=1)
        if token:
            return token, 'observe-window'
        state = await dump_page(page, 'turnstile-observe')
        last_continue_enabled = any(
            (b["text"] or "").strip().upper() == "CONTINUE" and not b["disabled"]
            for b in state["buttons"]
        )
        if last_continue_enabled:
            return state.get('turnstile') or '', 'continue-enabled-no-token-read'

    if nudge_after > 0:
        nudge = await body_nudge(page)
        print(f"    轻 nudge: {nudge}")
        start_nudge = time.time()
        while time.time() - start_nudge < nudge_after:
            token = await wait_turnstile(page, timeout=1)
            if token:
                return token, 'after-nudge'
            state = await dump_page(page, 'turnstile-after-nudge')
            last_continue_enabled = any(
                (b["text"] or "").strip().upper() == "CONTINUE" and not b["disabled"]
                for b in state["buttons"]
            )
            if last_continue_enabled:
                return state.get('turnstile') or '', 'continue-enabled-after-nudge'

    if final_timeout > 0:
        token = await wait_turnstile(page, timeout=final_timeout)
        if token:
            return token, 'final-tail'

    return None, 'not-ready'


async def wait_code_input(page, timeout=20):
    timeout = normalize_timeout(timeout, minimum=0)
    for _ in range(timeout):
        r = await page.evaluate(
            """(function(){
            for (var i of document.querySelectorAll('input')) {
                if (i.offsetParent!==null) {
                    var p=(i.placeholder||'').toLowerCase(), n=(i.name||'').toLowerCase();
                    if (p.includes('code')||p.includes('verification')||p.includes('otp')||n.includes('code')||n.includes('otp'))
                        return i.placeholder||i.name||'code-input';
                }
            }
            return null;
        })()"""
        )
        if r:
            return r
        await asyncio.sleep(1)
    return None


async def query_input(page, selector, timeout=10):
    timeout = normalize_timeout(timeout, minimum=0)
    for _ in range(timeout):
        el = await page.query_selector(selector)
        if el:
            return el
        await asyncio.sleep(1)
    return None


async def inspect_post_continue(page):
    info = await page.evaluate(
        """(function(){
        var body = (document.body.innerText || '');
        var lower = body.toLowerCase();
        var result = {
            url: location.href,
            bodyTop: body.substring(0, 1200),
            hasCodeInput: false,
            hasForm: false,
            continueEnabled: false,
            tokenLen: 0,
        };
        for (var i of document.querySelectorAll('input')) {
            if (i.offsetParent !== null) {
                var p = (i.placeholder || '').toLowerCase();
                var n = (i.name || '').toLowerCase();
                if (p.includes('code') || p.includes('verification') || p.includes('otp') || n.includes('code') || n.includes('otp')) result.hasCodeInput = true;
                if (n === 'email' || n === 'username' || i.type === 'password') result.hasForm = true;
            }
        }
        for (var b of document.querySelectorAll('button')) {
            if (b.offsetParent !== null && ((b.innerText || '').trim().toUpperCase() === 'CONTINUE') && !b.disabled) result.continueEnabled = true;
        }
        var t = document.querySelector('input[name="cf-turnstile-response"]');
        result.tokenLen = t && t.value ? t.value.length : 0;
        if (lower.includes('risk-detection') || lower.includes('registration denied due to security')) result.outcome = 'risk_detected';
        else if (lower.includes('account exists')) result.outcome = 'account_exists';
        else if (result.hasCodeInput) result.outcome = 'code_input';
        else if (result.hasForm) result.outcome = 'form';
        else result.outcome = 'other';
        return JSON.stringify(result);
    })()"""
    )

    if isinstance(info, tuple):
        info = info[0]
    if isinstance(info, (bytes, bytearray)):
        info = info.decode("utf-8", errors="ignore")
    if not isinstance(info, str):
        return {
            "url": "", "bodyTop": "", "hasCodeInput": False, "hasForm": False,
            "continueEnabled": False, "tokenLen": 0,
            "outcome": f"inspect_non_string:{type(info).__name__}",
        }
    try:
        return json.loads(info)
    except Exception:
        return {
            "url": "", "bodyTop": str(info)[:300], "hasCodeInput": False, "hasForm": False,
            "continueEnabled": False, "tokenLen": 0, "outcome": "inspect_parse_failed",
        }


def append_post_continue_trace(event, state):
    if not POST_CONTINUE_TRACE:
        return
    row = {
        "ts": round(time.time(), 3),
        "label": POST_CONTINUE_TRACE_LABEL or MATRIX_TAG,
        "event": event,
        "state": state,
    }
    try:
        with open(POST_CONTINUE_TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"    ⚠️ post-continue trace append failed: {e}")


async def wait_post_continue_outcome(page, timeout=12):
    last = None
    saw_risk = None
    for idx in range(timeout):
        state = await inspect_post_continue(page)
        last = state
        append_post_continue_trace(f"tick_{idx+1}", state)
        outcome = state.get("outcome")
        if outcome == "risk_detected":
            saw_risk = state
        if outcome == "account_exists":
            return outcome, state
        if outcome == "code_input":
            return outcome, state
        await asyncio.sleep(1)
    if saw_risk:
        return "risk_detected", saw_risk
    return (last or {}).get("outcome", "timeout"), last or {}


async def human_fill(el, text):
    await el.focus()
    try:
        await el.clear_input()
    except Exception:
        pass
    await asyncio.sleep(0.3 + random.random() * 0.3)
    if TYPING_MODE == 'charwise':
        for ch in text:
            await el.send_keys(ch)
            await asyncio.sleep(0.08 + random.random() * 0.12)  # 更慢更自然
    elif TYPING_MODE == 'charwise_slow':
        for ch in text:
            await el.send_keys(ch)
            await asyncio.sleep(0.15 + random.random() * 0.20)
    else:
        await el.send_keys(text)
    await asyncio.sleep(1.5 + random.random() * 1.0)  # 更长的字段后停留


# ── 注册流子步骤（register.py 调用）─────────────────────────────

async def ensure_invite_checkbox(page):
    result = await page.evaluate(
        """(function(){
        var boxes = [...document.querySelectorAll('input[type="checkbox"]')].filter(function(c){ return c.offsetParent !== null; });
        var checkedBefore = boxes.filter(function(c){ return !!c.checked; }).length;
        boxes.forEach(function(c){ if(!c.checked) c.click(); });
        var checkedAfter = boxes.filter(function(c){ return !!c.checked; }).length;
        return JSON.stringify({count: boxes.length, checked_before: checkedBefore, checked_after: checkedAfter});
    })()"""
    )
    return json.loads(result)


async def reopen_signup_form(page):
    await ensure_invite_checkbox(page)
    await asyncio.sleep(0.5)
    r = await click_btn(page, ["sign up with email"], timeout=6)
    await asyncio.sleep(2)
    return r


async def resolve_form_inputs(page):
    email_el = await query_input(page, 'input[name="email"]', timeout=4)
    user_el = await query_input(page, 'input[name="username"]', timeout=4)
    pass_el = await query_input(page, 'input[type="password"]', timeout=4)
    return email_el, user_el, pass_el


async def force_refill_for_token_attempt(page, email, prefix):
    email_el, user_el, pass_el = await resolve_form_inputs(page)
    if not (email_el and user_el and pass_el):
        return False, "form_missing"
    await human_fill(email_el, email)
    await asyncio.sleep(0.2)  # INTER_FIELD_GAP_SECONDS
    await human_fill(user_el, prefix)
    await asyncio.sleep(0.2)
    await human_fill(pass_el, PASSWORD)
    await asyncio.sleep(max(0.3, 0.8 / 2))  # POST_FIELD_SETTLE_SECONDS / 2
    return True, "refilled"


# ── CDP 网络监控 ──────────────────────────────────────────────────────────────

class NetworkMonitor:
    """CDP 级网络监控：不依赖页面注入时序，直接从 DevTools Protocol 捕获所有请求/响应。

    用法:
        monitor = NetworkMonitor()
        await monitor.start(page)
        ...  # 执行注册流程
        events = monitor.stop()
        # events["requests"] / events["responses"] / events["pre_register"]
    """

    def __init__(self):
        self._requests = {}   # request_id → request dict
        self._responses = []  # list of response dicts
        self._bodies = {}     # request_id → response body (from getResponseBody)
        self._pre_register_events = []  # 特别标记 /auth/local/pre-register
        self._preregister_requests = []
        self._enabled = False
        self._tab = None

    async def start(self, page):
        """开启 CDP 网络监控并注册事件监听器"""
        self._tab = page
        tab = page

        # 启用 Network domain
        await tab.send(uc.cdp.network.enable())

        # 注册 requestWillBeSent 监听器
        def on_request(event):
            req = event.request
            self._requests[event.request_id] = {
                "request_id": event.request_id,
                "url": req.url,
                "method": req.method,
                "post_data": req.post_data,
                "timestamp": time.time(),
                "type": str(event.type_) if event.type_ else None,
                "wall_time": event.wall_time,
            }
            if "pre-register" in req.url or "auth/local" in req.url:
                self._pre_register_events.append({
                    "event": "request",
                    "url": req.url,
                    "method": req.method,
                    "post_data": req.post_data[:500] if req.post_data else None,
                    "timestamp": time.time(),
                })
                self._preregister_requests.append(event.request_id)

        tab.add_handler(uc.cdp.network.RequestWillBeSent, on_request)

        # 注册 responseReceived 监听器
        def on_response(event):
            resp = event.response
            entry = {
                "request_id": event.request_id,
                "url": resp.url,
                "status": resp.status,
                "status_text": resp.status_text,
                "headers": resp.headers,
                "mimeType": resp.mime_type,
                "timestamp": time.time(),
            }
            self._responses.append(entry)

            if "pre-register" in resp.url or "auth/local" in resp.url:
                self._pre_register_events.append({
                    "event": "response",
                    "url": resp.url,
                    "status": resp.status,
                    "timestamp": time.time(),
                    "headers": dict(resp.headers) if resp.headers else {},
                })

        tab.add_handler(uc.cdp.network.ResponseReceived, on_response)

        # 注册 loadingFailed 监听器
        def on_loading_failed(event):
            if "challenges.cloudflare" in (event.error_text or "") or "turnstile" in (event.error_text or ""):
                self._pre_register_events.append({
                    "event": "loading_failed",
                    "error": event.error_text,
                    "blocked_reason": str(event.blocked_reason) if event.blocked_reason else None,
                    "timestamp": time.time(),
                })

        tab.add_handler(uc.cdp.network.LoadingFailed, on_loading_failed)

        self._enabled = True
        print("    [cdp-net] CDP 网络监控已开启")

    def get_pregister_events(self):
        """获取 /auth/local/pre-register 相关的网络事件"""
        return self._pre_register_events

    def get_pregister_response(self):
        """获取 pre-register 请求的响应（如果有的话）"""
        for resp in self._responses:
            if "pre-register" in resp["url"]:
                return resp
        return None

    def summary(self):
        """生成网络监控摘要"""
        cf_loaded = [r for r in self._responses if "challenges.cloudflare" in r["url"]]
        cf_failed = [e for e in self._pre_register_events if e.get("event") == "loading_failed"]
        return {
            "total_requests": len(self._requests),
            "total_responses": len(self._responses),
            "cf_resources_loaded": len(cf_loaded),
            "cf_resources_failed": len(cf_failed),
            "pregister_events": len(self._pre_register_events),
            "pregister_url_hits": [
                {"url": e.get("url", ""), "status": e.get("status", e.get("method", ""))}
                for e in self._pre_register_events if e.get("event") == "response"
            ],
        }

    def dump(self):
        """打印网络监控摘要"""
        s = self.summary()
        print(f"    [cdp-net] 总请求数: {s['total_requests']}, 总响应数: {s['total_responses']}")
        print(f"    [cdp-net] Cloudflare 资源: {s['cf_resources_loaded']} loaded, {s['cf_resources_failed']} failed")
        print(f"    [cdp-net] pre-register 事件: {s['pregister_events']}")
        for hit in s["pregister_url_hits"]:
            print(f"    [cdp-net]   → {hit['url']} [{hit['status']}]")

    def stop(self):
        """返回所有捕获的事件并清理"""
        return {
            "requests": list(self._requests.values()),
            "responses": self._responses[:],
            "pre_register": self._pre_register_events[:],
            "summary": self.summary(),
        }


# ── 指纹注入（CDP 级，不依赖启动参数）─────────────────────────────────────────

# navigator.plugins / WebGL 假数据
FAKE_PLUGINS_JS = """(function(){
    // 伪装 navigator.webdriver = false（深度覆盖）
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    if (navigator.permissions) {
        var origQuery = navigator.permissions.query.bind(navigator.permissions);
        navigator.permissions.query = function(params) {
            if (params.name === 'notifications') {
                return Promise.resolve({state: 'prompt', onchange: null});
            }
            return origQuery(params);
        };
    }

    // 伪装 navigator.plugins（Chrome 典型插件集）
    var plugins = [
        {name: 'PDF Viewer', description: 'Portable Document Format', filename: 'internal-pdf-viewer', 0: {type: 'application/pdf', suffixes: 'pdf'}},
        {name: 'Chrome PDF Viewer', description: 'Portable Document Format', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', 0: {type: 'application/pdf', suffixes: 'pdf'}},
        {name: 'Chromium PDF Viewer', description: 'Portable Document Format', filename: 'internal-pdf-viewer', 0: {type: 'application/pdf', suffixes: 'pdf'}},
    ];
    var PluginArray = function(){};
    PluginArray.prototype = Object.create(navigator.plugins.__proto__);
    PluginArray.prototype.length = plugins.length;
    for (var i = 0; i < plugins.length; i++) {
        (function(p, idx){
            Object.defineProperty(PluginArray.prototype, idx, {get: function(){return p;}});
            Object.defineProperty(PluginArray.prototype, p.name, {get: function(){return p;}});
        })(plugins[i], i);
    }
    PluginArray.prototype.item = function(i) { return plugins[i] || null; };
    PluginArray.prototype.namedItem = function(n) { for (var p of plugins) if (p.name === n) return p; return null; };
    PluginArray.prototype[Symbol.iterator] = function() { return plugins[Symbol.iterator](); };
    Object.defineProperty(navigator, 'plugins', {get: function(){ return new PluginArray(); }});

    // 伪装 navigator.languages
    Object.defineProperty(navigator, 'languages', {get: function() {
        var lang = document.documentElement.lang || navigator.language || 'en-US';
        return [lang, lang.split('-')[0]];
    }});

    // 伪装 WebGL renderer（避开虚拟机指纹）
    var getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(p) {
        if (p === 37445) return 'ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)';
        if (p === 37446) return 'Google Inc. (Intel)';
        return getParameter.call(this, p);
    };

    // Chrome runtime 伪装（防止缺少 chrome.runtime 检测）
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || {
        PlatformOs: {MAC:'mac', WIN:'win', ANDROID:'android', CROS:'cros', LINUX:'linux', OPENBSD:'openbsd'},
        PlatformArch: {ARM:'arm', X86_32:'x86-32', X86_64:'x86-64', MIPS:'mips', MIPS64:'mips64'},
        PlatformNaclArch: {ARM:'arm', X86_32:'x86-32', X86_64:'x86-64', MIPS:'mips', MIPS64:'mips64'},
        RequestUpdateCheckStatus: {THROTTLED:'throttled', NO_UPDATE:'no_update', UPDATE_AVAILABLE:'update_available'},
        OnInstalledReason: {INSTALL:'install', UPDATE:'update', CHROME_UPDATE:'chrome_update', SHARED_MODULE_UPDATE:'shared_module_update'},
        OnRestartRequiredReason: {APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic'},
    };

    // CDP 检测：navigator.connection.type 防空
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'type', {get: function() { return 'wifi'; }});
    }
})();"""


async def inject_fingerprint(page):
    """通过 CDP 注入指纹伪装脚本（在页面 JS 执行前生效）"""
    try:
        # addScriptToEvaluateOnNewDocument 确保脚本在每个页面/iframe 的最早时机执行
        await page.send(uc.cdp.page.add_script_to_evaluate_on_new_document(
            source=FAKE_PLUGINS_JS,
            run_immediately=True,
        ))
        print("    [fingerprint] CDP 级指纹注入完成（webdriver/plugins/WebGL/chrome.runtime）")
    except Exception as e:
        print(f"    [fingerprint] 注入失败: {e}")


# ── EzSolver 风格 Turnstile 注入（来自 EzSolver 项目）─────────────────────────────────────────

async def inject_turnstile_widget(page, sitekey: str, timeout: int = 45, force_new: bool = False) -> str:
    """EzSolver 风格：注入 Turnstile widget 到页面 DOM，等待 token 就绪
    
    流程：
    1. 清理旧的 Turnstile 元素（如果存在）
    2. 创建固定位置的 div 容器 #_ts_box
    3. 动态加载 Turnstile API 脚本（带 onload callback + render=explicit）
    4. 在 callback 里存储 token 到 window._tsToken
    5. 支持自动 invisible 模式 + 手动 checkbox 点击
    6. 多轮点击重试（最多 3 次）
    
    返回 token 字符串，失败抛出 TimeoutError
    """
    
    # 清理旧的注入（如果 force_new 或已存在但可能损坏）
    if force_new:
        await page.evaluate("""
            (() => {
                // 移除旧的 _ts_box 容器
                const oldBox = document.getElementById('_ts_box');
                if (oldBox) oldBox.remove();
                // 移除旧的 Turnstile 脚本
                document.querySelectorAll('script[src*="turnstile"]').forEach(s => s.remove());
                // 清理全局变量
                if (window._tsToken) window._tsToken = null;
                if (window._tsLoad) window._tsLoad = null;
                // 移除已有的 Turnstile iframe
                document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]').forEach(f => f.remove());
                // 移除已有的 cf-turnstile-response inputs
                document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(inp => inp.value = '');
            })()
        """)
        await asyncio.sleep(0.5)
    
    # 注入 widget 到页面 DOM
    inject_r = await page.evaluate(f"""
        (() => {{
            // 检查是否已经存在
            if (document.getElementById('_ts_box')) return 'already';
            
            window._tsToken = null;
            const wrap = document.createElement('div');
            wrap.id = '_ts_box';
            wrap.style = 'position:fixed;top:20px;left:20px;z-index:2147483647;';
            document.body.appendChild(wrap);
            window._tsLoad = function () {{
                turnstile.render('#_ts_box', {{
                    sitekey: '{sitekey}',
                    callback: function(token) {{ 
                        window._tsToken = token; 
                        // 同步到所有 cf-turnstile-response input
                        document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(function(inp) {{
                            inp.value = token;
                        }});
                    }}
                }});
            }};
            const s = document.createElement('script');
            s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_tsLoad&render=explicit';
            s.async = true;
            document.head.appendChild(s);
            return 'injected';
        }})()
    """)
    print(f"    [turnstile] inject: {inject_r}")
    
    # 等待 Turnstile 脚本加载
    await asyncio.sleep(5.0)
    
    async def get_token():
        """获取当前 token 并同步到表单 input"""
        token = await page.evaluate("""
            (() => {
                if (window._tsToken) return window._tsToken;
                const inp = document.querySelector('#_ts_box [name="cf-turnstile-response"]');
                return (inp && inp.value) ? inp.value : null;
            })()
        """)
        if token:
            # 同步完整的 token 到所有 cf-turnstile-response input，并触发事件
            await page.evaluate("""
                (() => {
                    const token = window._tsToken;
                    if (!token) return;
                    document.querySelectorAll('input[name="cf-turnstile-response"]').forEach(function(inp) {
                        inp.value = token;
                        // 触发 change 和 input 事件通知前端
                        inp.dispatchEvent(new Event('change', { bubbles: true }));
                        inp.dispatchEvent(new Event('input', { bubbles: true }));
                    });
                    // 额外触发 turnstile 回调（如果页面有自定义处理）
                    if (window.onTurnstileSuccess) {
                        try { window.onTurnstileSuccess(token); } catch(e) {}
                    }
                })()
            """)
        return token
    
    async def get_cf_iframe_rect():
        """获取 Cloudflare iframe 的位置和尺寸"""
        raw = await page.evaluate("""
            JSON.stringify((() => {
                for (const f of document.querySelectorAll('iframe')) {
                    const src = f.src || f.getAttribute('src') || '';
                    if (!src.includes('challenges.cloudflare.com')) continue;
                    const r = f.getBoundingClientRect();
                    if (r.width > 50 && r.height > 20) return {x:r.x, y:r.y, w:r.width, h:r.height};
                }
                return null;
            })())
        """)
        if raw and raw != 'null':
            return json.loads(raw)
        return None
    
    async def do_click(rect):
        """人类化点击 checkbox"""
        if rect:
            cx = rect["x"] + 28 + random.uniform(-3, 3)
            cy = rect["y"] + rect["h"] / 2 + random.uniform(-3, 3)
            print(f"    [turnstile] 点击 iframe at ({cx:.0f}, {cy:.0f})")
        else:
            # Widget 固定在 top:20px left:20px
            cx = 20 + 28 + random.uniform(-3, 3)
            cy = 20 + 32 + random.uniform(-3, 3)
            print(f"    [turnstile] iframe 未找到，点击固定位置 ({cx:.0f}, {cy:.0f})")
        
        # 人类化鼠标移动
        await page.mouse_move(cx - 80, cy - 20)
        await asyncio.sleep(random.uniform(0.15, 0.25))
        await page.mouse_move(cx, cy)
        await asyncio.sleep(random.uniform(0.08, 0.15))
        await page.mouse_click(cx, cy)
    
    # 检查是否已自动解决（invisible 模式）
    token = await get_token()
    if token:
        print(f"    [turnstile] ✅ token 自动就绪 len={len(token)}")
        return token
    
    # 等待 iframe 出现
    print("    [turnstile] 等待 iframe...")
    rect = None
    for _ in range(20):
        rect = await get_cf_iframe_rect()
        if rect:
            print(f"    [turnstile] iframe 位置: x={rect['x']}, y={rect['y']}, w={rect['w']}, h={rect['h']}")
            break
        await asyncio.sleep(0.5)
    
    # 点击循环：点击、等待、重试最多 3 次
    deadline = asyncio.get_event_loop().time() + timeout
    click_count = 0
    last_click = 0.0
    
    while asyncio.get_event_loop().time() < deadline:
        token = await get_token()
        if token:
            print(f"    [turnstile] ✅ token 就绪 len={len(token)}")
            return token
        
        now = asyncio.get_event_loop().time()
        if click_count == 0 or (now - last_click > 8):
            if click_count >= 3:
                await asyncio.sleep(0.3)
                continue
            await do_click(rect)
            last_click = asyncio.get_event_loop().time()
            click_count += 1
            # 点击后刷新 iframe 位置（可能移动）
            await asyncio.sleep(1.0)
            rect = await get_cf_iframe_rect() or rect
            continue
        
        await asyncio.sleep(0.3)
    
    raise TimeoutError(f"Turnstile token not obtained within {timeout}s")


async def ensure_turnstile_token(page, sitekey: str, max_attempts: int = 3, timeout_per_attempt: int = 30) -> tuple:
    """确保获取 Turnstile token，支持多次尝试
    
    返回: (token, continue_enabled, readiness, state)
    """
    for attempt in range(1, max_attempts + 1):
        print(f"    [turnstile] 尝试 {attempt}/{max_attempts}...")
        
        try:
            # 第 2 次及以后尝试时，强制清理旧注入
            force_new = attempt > 1
            token = await inject_turnstile_widget(page, sitekey, timeout=timeout_per_attempt, force_new=force_new)
            if token and len(token) > 20:
                # 检查 Continue 按钮状态
                state = await dump_page(page, f"turnstile-{attempt}")
                continue_enabled = any(
                    (b.get("text") or "").strip().upper() == "CONTINUE" and not b.get("disabled")
                    for b in state.get("buttons", [])
                )
                return token, continue_enabled, f"attempt-{attempt}", state
        except TimeoutError as e:
            print(f"    [turnstile] ⚠️ {e}")
            state = await dump_page(page, f"turnstile-timeout-{attempt}")
            continue_enabled = any(
                (b.get("text") or "").strip().upper() == "CONTINUE" and not b.get("disabled")
                for b in state.get("buttons", [])
            )
            if continue_enabled:
                # Continue 按钮已启用，即使没有 token 也继续
                token = state.get("turnstile") or ""
                return token, True, f"continue-enabled-attempt-{attempt}", state
        except Exception as e:
            print(f"    [turnstile] ❌ {e}")
        
        if attempt < max_attempts:
            print(f"    [turnstile] 重试...")
            await asyncio.sleep(2)
    
    state = await dump_page(page, "turnstile-final")
    token = state.get("turnstile") or ""
    continue_enabled = any(
        (b.get("text") or "").strip().upper() == "CONTINUE" and not b.get("disabled")
        for b in state.get("buttons", [])
    )
    return token or None, continue_enabled, "exhausted", state
