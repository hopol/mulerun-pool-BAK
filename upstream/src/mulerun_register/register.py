#!/usr/bin/env python3
"""
MuleRun 批量注册 v6 - 入口模块
依赖: config.py (配置) | email_client.py (邮箱) | browser_helpers.py (浏览器)
"""

import asyncio
import json
import os
import time

import nodriver as uc

from .config import (
    INVITE_URL,
    OUTLOOK_ACCOUNTS,
    PASSWORD,
    MAX_TOKEN_ATTEMPTS,
    VERIFICATION_TIMEOUT,
    RESULT_FILE,
    MAX_CONCURRENCY,
    RUN_ID,
    SCREENSHOT_DIR,
    LOG_FILE,
    PROXY_SERVER,
    PROXY_POOL,
    get_next_proxy,
    get_healthy_proxy,
    check_proxy_health,
    reset_proxy_index,
    FRESH_PROFILE,
    LANG_OVERRIDE,
    INTER_FIELD_GAP_SECONDS,
    POST_CONTINUE_WINDOW_SECONDS,
    PRE_SUBMIT_OBSERVE_SECONDS,
    INVITE_SETTLE_SECONDS,
    FORM_SETTLE_SECONDS,
    TURNSTILE_OBSERVE_SECONDS,
    TURNSTILE_NUDGE_SECONDS,
    TURNSTILE_FINAL_SECONDS,
    CHROME_PATH,
    HEADLESS,
    compute_user_data_dir,
    build_browser_args,
    build_fingerprint_args,
    normalize_timeout,
)
from .email_client import (
    gen_email_async,
    poll_code,
    check_worker_health,
    log_step,
)
from .browser_helpers import (
    ss_path,
    dump_page,
    click_btn,
    query_input,
    wait_code_input,
    wait_turnstile_ready,
    wait_post_continue_outcome,
    apply_warmup_profile,
    apply_trace_profile,
    ensure_invite_checkbox,
    reopen_signup_form,
    resolve_form_inputs,
    force_refill_for_token_attempt,
    human_fill,
    NetworkMonitor,
    inject_fingerprint,
    inject_turnstile_widget,
    ensure_turnstile_token,
)


# ── 注册流程 ─────────────────────────────────────────────────────

async def register_one(idx, total, invite_url, proxy_override=None):
    email, prefix = await gen_email_async()
    if not email:
        return False, "gen_email_failed", ""

    # 🎯 代理轮询 + 健康检查
    if proxy_override:
        proxy = proxy_override
    else:
        try:
            proxy = get_healthy_proxy()
        except RuntimeError as e:
            print(f"    ❌ {e}")
            return False, "all_proxies_dead", email
    log_step("start", idx=idx, email=email, proxy=proxy)
    print(f"\n{'='*50}")
    print(f"[{idx}/{total}] 🚀 {email}")
    print(f"{'='*50}")
    print(f"    transport: proxy={proxy or 'direct'} fresh_profile={FRESH_PROFILE} lang={LANG_OVERRIDE}")

    user_data_dir = compute_user_data_dir()
    browser_args = build_browser_args(proxy_override=proxy)

    # 🎯 指纹随机化
    fingerprint_args, lang, tz = build_fingerprint_args()
    browser_args.extend(fingerprint_args)
    print(f"    fingerprint: lang={lang}, tz={tz}")

    browser = await uc.start(
        config=uc.Config(
            user_data_dir=user_data_dir,
            sandbox=False,
            no_sandbox=True,
            headless=HEADLESS,
            browser_executable_path=CHROME_PATH,
            browser_args=browser_args,
            lang=lang,
        )
    )

    # 🎯 CDP 级指纹注入（在任何页面加载之前）
    page = await browser.get("about:blank")
    await inject_fingerprint(page)

    # 🎯 启动 CDP 网络监控
    monitor = NetworkMonitor()
    await monitor.start(page)

    try:
        # [1] 打开邀请页
        print("[1] 打开邀请页...")
        page = await browser.get(invite_url)
        await asyncio.sleep(INVITE_SETTLE_SECONDS)
        await dump_page(page, "invite")
        await apply_warmup_profile(page)
        await apply_trace_profile(page, 'invite-loaded')

        # [2] 处理邀请页协议
        print("[2] 处理邀请页协议...")
        cb_state = await ensure_invite_checkbox(page)
        print(f"    checkbox={cb_state}")
        await asyncio.sleep(1)
        await dump_page(page, "invite-after-checkbox")

        # [3] 点击 Sign up with Email
        print("[3] 点击 Sign up with Email...")
        await apply_trace_profile(page, 'before-signup-click')
        r = await click_btn(page, ["sign up with email"], timeout=10)
        print(f"    {r}")
        await asyncio.sleep(FORM_SETTLE_SECONDS)
        form = await dump_page(page, "form")

        # [4] 确认三字段表单
        print("[4] 确认三字段表单...")
        email_el, user_el, pass_el = await resolve_form_inputs(page)
        if not (email_el and user_el and pass_el):
            print("    首次未进入三字段表单，重试一次入口点击...")
            r2 = await reopen_signup_form(page)
            print(f"    retry={r2}")
            await asyncio.sleep(2)
            await dump_page(page, "form-retry")
            email_el, user_el, pass_el = await resolve_form_inputs(page)
        if not (email_el and user_el and pass_el):
            await page.save_screenshot(ss_path(f"{idx:02d}_missing_form"))
            return False, "form_not_ready", email

        # [5-7] 输入三字段
        print("[5] 真实输入 email...")
        await apply_trace_profile(page, 'before-email')
        await human_fill(email_el, email)
        await dump_page(page, "after-email")

        print("[6] 真实输入 username...")
        await apply_trace_profile(page, 'before-username')
        await asyncio.sleep(INTER_FIELD_GAP_SECONDS)
        await human_fill(user_el, prefix)
        await dump_page(page, "after-username")

        print("[7] 真实输入 password...")
        await apply_trace_profile(page, 'before-password')
        await asyncio.sleep(INTER_FIELD_GAP_SECONDS)
        await human_fill(pass_el, PASSWORD)
        await dump_page(page, "after-password")
        await page.save_screenshot(ss_path(f"{idx:02d}_after_password"))

        # [8] EzSolver 风格拿 token（多次尝试）
        print("[8] EzSolver 风格拿 token...")
        SITEKEY = "0x4AAAAAABnlT9BAdgkYH1Yp"
        
        token, continue_enabled, readiness, state_wait = await ensure_turnstile_token(
            page, SITEKEY, max_attempts=MAX_TOKEN_ATTEMPTS, timeout_per_attempt=30
        )
        if token:
            print(f"    ✅ token={str(token)[:30]}... ({readiness})")
        else:
            print(f"    ⚠️ token 仍为空 ({readiness})")
        await page.save_screenshot(ss_path(f"{idx:02d}_continue_gate"))
        if not continue_enabled:
            return False, f"continue_disabled:{readiness}", email

        # [9] 点击 Continue
        print("[9] 点击 Continue...")
        await apply_trace_profile(page, 'before-continue')
        if PRE_SUBMIT_OBSERVE_SECONDS > 0:
            print(f"    submit 前观察 {PRE_SUBMIT_OBSERVE_SECONDS}s")
            await asyncio.sleep(PRE_SUBMIT_OBSERVE_SECONDS)
        r = await click_btn(page, ["continue"], timeout=8)
        print(f"    {r}")
        outcome, outcome_state = await wait_post_continue_outcome(page, timeout=POST_CONTINUE_WINDOW_SECONDS)
        await page.save_screenshot(ss_path(f"{idx:02d}_after_continue"))

        # 🎯 CDP 网络监控 dump（pre-register 响应捕获）
        monitor.dump()
        preg_resp = monitor.get_pregister_response()
        if preg_resp:
            print(f"    [cdp-net] 🔍 pre-register 响应: HTTP {preg_resp['status']}")
            # 如果 CDP 捕获到 400/403，直接返回（比页面状态更快）
            if preg_resp["status"] == 400:
                return False, "pre_register_400", email
            if preg_resp["status"] == 403:
                return False, "pre_register_403", email
        else:
            print(f"    [cdp-net] ⚠️ 未捕获到 pre-register 响应")

        state_after_continue = await dump_page(page, "after-continue")

        body = (state_after_continue.get("text", "") + "\n" + outcome_state.get("bodyTop", ""))
        lower_body = body.lower()
        if outcome == "risk_detected" or "risk-detection" in lower_body or "registration denied due to security" in lower_body:
            return False, "risk_detected", email
        if "account exists" in lower_body or outcome == "account_exists":
            return False, "account_exists", email

        # 表单掉回重试
        if outcome == "form":
            print("    ⚠️ Continue 后掉回表单页，执行一次同页补提...")
            token2, continue_enabled2, readiness2, state_retry = await acquire_turnstile_token(
                page, email, prefix, max_attempts=2
            )
            if token2:
                print(f"    ✅ retry token={str(token2)[:30]}... ({readiness2})")
            else:
                print(f"    ⚠️ retry token 仍为空 ({readiness2})")
            if not continue_enabled2:
                await page.save_screenshot(ss_path(f"{idx:02d}_no_code_input_retry"))
            r2 = await click_btn(page, ["continue"], timeout=8)
            print(f"    retry-continue={r2}")
            outcome, outcome_state = await wait_post_continue_outcome(page, timeout=POST_CONTINUE_WINDOW_SECONDS)
            await page.save_screenshot(ss_path(f"{idx:02d}_after_retry_continue"))
            state_after_continue = await dump_page(page, "after-retry-continue")
            body = (state_after_continue.get("text", "") + "\n" + outcome_state.get("bodyTop", ""))
            if "account exists" in body.lower() or outcome == "account_exists":
                return False, "account_exists", email

        # [10] 检查验证码框
        print("[10] 检查验证码框...")
        code_ph = await wait_code_input(page, timeout=12)
        if not code_ph:
            await page.save_screenshot(ss_path(f"{idx:02d}_no_code_input"))
            return False, "no_code_input", email
        print(f"    ✅ 验证码框: {code_ph}")

        # [11] 点 Get Code
        print("[11] 点 Get Code（如存在）...")
        r = await click_btn(page, ["get code", "send code"], timeout=5)
        print(f"    {r or '无按钮（可能已自动发送）'}")
        await asyncio.sleep(2)

        # [12] 等验证码
        print("[12] 等验证码...")
        code = await poll_code(email, VERIFICATION_TIMEOUT)
        if not code:
            await page.save_screenshot(ss_path(f"{idx:02d}_code_timeout"))
            return False, "no_code", email

        # [13] 回填验证码
        print(f"[13] 回填验证码: {code}")
        code_el = None
        for selector in [
            'input[name*="code"]',
            'input[name*="otp"]',
            'input[placeholder*="code" i]',
            'input[placeholder*="verification" i]',
        ]:
            code_el = await page.query_selector(selector)
            if code_el:
                break
        if not code_el:
            return False, "code_input_missing_late", email
        await human_fill(code_el, code)

        # [14] 提交验证码
        print("[14] 提交验证码...")
        r = await click_btn(page, ["verify", "continue", "submit", "confirm", "sign up"], timeout=8)
        print(f"    {r}")
        await asyncio.sleep(10)
        await page.save_screenshot(ss_path(f"{idx:02d}_final"))
        final = await dump_page(page, "final")

        final_url = final["url"]
        final_body = final.get("text", "")
        if "signup" not in final_url.lower() or "dashboard" in final_url.lower() or "welcome" in final_body.lower():
            return True, "success", email

        return False, "stuck_after_submit", email

    except Exception as e:
        print(f"❌ {e}")
        import traceback
        traceback.print_exc()
        return False, str(e), email
    finally:
        try:
            # 🎯 最终网络监控汇总
            monitor.dump()
            events = monitor.stop()
            preg_events = events.get("pre_register", [])
            if preg_events:
                print(f"    [cdp-net] 📊 pre-register 全量事件:")
                for ev in preg_events:
                    print(f"    [cdp-net]   {ev}")
        except Exception:
            pass
        try:
            browser.stop()
        except Exception:
            pass


# 失败重试相关配置
RETRY_PROXY_ON_ERRORS = ["continue_disabled", "no_code_input", "form_not_ready"]
MAX_RETRIES_PER_PROXY = 2


async def register_one_safe(idx, total, invite_url, results, results_lock, sem):
    """带 semaphore 的安全包装 + 失败换代理重试"""
    async with sem:
        # 获取可用代理池
        from .config import PROXY_POOL
        
        # 第一轮：使用当前代理
        proxy_override = None
        last_error = None
        
        for attempt in range(MAX_RETRIES_PER_PROXY):
            try:
                # 动态切换代理（仅第二、三次尝试时切换）
                if attempt > 0 and PROXY_POOL:
                    import random
                    # 排除当前使用的代理，选择另一个
                    other_proxies = [p for p in PROXY_POOL if p != proxy_override]
                    if other_proxies:
                        proxy_override = random.choice(other_proxies)
                        print(f"    🔄 重试切换代理: {proxy_override}")
                    else:
                        proxy_override = None
                
                ok, detail, email = await register_one(
                    idx, total, invite_url, 
                    proxy_override=proxy_override
                )
                last_error = detail
                
                # 检查是否需要换代理重试
                should_retry = False
                for err_type in RETRY_PROXY_ON_ERRORS:
                    if err_type in str(detail):
                        should_retry = True
                        break
                
                if ok:
                    # 成功，直接返回
                    async with results_lock:
                        results.append({"idx": idx, "email": email, "ok": ok, "detail": detail})
                    return ok, detail, email
                elif should_retry and attempt < MAX_RETRIES_PER_PROXY - 1:
                    # 失败但可重试，换代理再试
                    print(f"    ⚠️ 遇到可重试错误 {detail}，尝试切换代理重试...")
                    continue
                else:
                    # 成功或不可重试，退出
                    break
                    
            except Exception as e:
                last_error = f"exception:{e}"
                if attempt < MAX_RETRIES_PER_PROXY - 1:
                    continue
                break
        
        # 最终结果
        ok = False
        email = ""
        async with results_lock:
            results.append({"idx": idx, "email": email, "ok": ok, "detail": last_error or "unknown"})
        return ok, last_error or "unknown", email


async def main():
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("-n", "--count", type=int, default=1)
    p.add_argument("-i", "--invite", type=str, default=INVITE_URL)
    p.add_argument("-c", "--concurrency", type=int, default=MAX_CONCURRENCY)
    args = p.parse_args()

    concurrency = args.concurrency
    sem = asyncio.Semaphore(concurrency)

    print(f"🎯 MuleRun v6 | {args.count}个 | 并行={concurrency} | run={RUN_ID}")
    print(f"   截图: {SCREENSHOT_DIR}/")
    print(f"   日志: {LOG_FILE}")
    print(f"   邀请: {args.invite}\n")

    log_step("run_start", idx=args.count, concurrency=concurrency)

    ok, note = check_worker_health()
    if not ok:
        print(f"⚠️ Email Worker 健康检查失败: {note}")
        print("   如需强制运行，设置 MR_SKIP_HEALTHCHECK=1")
        if not os.getenv("MR_SKIP_HEALTHCHECK", "").strip():
            return

    results = []
    results_lock = asyncio.Lock()

    if args.count == 1 or concurrency <= 1:
        for i in range(args.count):
            ok, detail, email = await register_one_safe(i + 1, args.count, args.invite, results, results_lock, sem)
            if i < args.count - 1:
                await asyncio.sleep(5)
    else:
        tasks = []
        for i in range(args.count):
            t = asyncio.create_task(
                register_one_safe(i + 1, args.count, args.invite, results, results_lock, sem)
            )
            tasks.append(t)
            if i < args.count - 1:
                await asyncio.sleep(2)
        await asyncio.gather(*tasks, return_exceptions=True)

    results.sort(key=lambda r: r["idx"])
    success = sum(1 for r in results if r["ok"])

    print(f"\n📊 {success}/{args.count} 成功 | run={RUN_ID}")
    for r in results:
        print(f"  {'✅' if r['ok'] else '❌'} [{r['idx']}] {r['email']} ({r['detail']})")

    report = {"run_id": RUN_ID, "count": args.count, "concurrency": concurrency,
              "success": success, "results": results,
              "screenshots": SCREENSHOT_DIR, "log": LOG_FILE}
    with open(RESULT_FILE, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"→ {RESULT_FILE}")

    # 追加成功账号到 Outlook 账号结果文件
    accounts_file = os.getenv("MR_ACCOUNTS_FILE", OUTLOOK_ACCOUNTS)
    newly_saved = 0
    for r in results:
        if r["ok"] and r.get("email"):
            line = f"{r['email']} | {PASSWORD} | confirmed | v6-auto-{RUN_ID} |\n"
            try:
                with open(accounts_file, "a", encoding="utf-8") as af:
                    af.write(line)
                newly_saved += 1
            except Exception:
                pass
    if newly_saved:
        print(f"📝 {newly_saved} 个新账号已追加到 {accounts_file}")

    log_step("run_end", success=success, total=args.count)


if __name__ == "__main__":
    # 显示环境优先级：DISPLAY > xvfb-run > headless
    if not os.getenv("DISPLAY") and not HEADLESS:
        import subprocess, sys, shutil
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            print("⚠️ 未检测到 DISPLAY，自动以 xvfb-run 启动...")
            argv = [os.path.abspath(sys.argv[0])] + sys.argv[1:]
            result = subprocess.run(
                [xvfb, "--auto-servernum", "--server-args=-screen 0 1920x1080x24"] + argv,
                stdin=subprocess.DEVNULL,
            )
            sys.exit(result.returncode)
        else:
            print("❌ 未检测到 DISPLAY 且 xvfb-run 不可用。")
            sys.exit(1)
    if HEADLESS:
        print("ℹ️ 未检测到 DISPLAY，自动使用 headless 模式")
    asyncio.run(main())
