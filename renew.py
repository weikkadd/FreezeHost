#!/usr/bin/env python3

import os
import re
import sys
import json
import base64
import traceback
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.parse import urljoin
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

DISCORD_TOKEN = os.environ.get("FREEZEHOST_DISCORD_TOKEN", "").strip()
TG_BOT_TOKEN  = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID    = os.environ.get("TG_CHAT_ID", "").strip()

TIMEOUT        = 60_000
MAX_SITE_RETRIES = 3
RETRY_WAIT     = 30_000          # ms between retries when site is down
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

BASE_URL   = "https://free.freezehost.pro"
VIEWPORT_W = 1280
VIEWPORT_H = 753

_SENSITIVE_VALUES: set[str] = set()
_SERVER_INDEX: dict[str, int] = {}

def _register_sensitive(*values):
    for v in values:
        if v and len(v) > 2:
            _SENSITIVE_VALUES.add(v)


def _server_label(server_id: str) -> str:
    if server_id not in _SERVER_INDEX:
        _SERVER_INDEX[server_id] = len(_SERVER_INDEX) + 1
    return f"服务器#{_SERVER_INDEX[server_id]}"


def _mask(text: str) -> str:
    if DISCORD_TOKEN:
        text = text.replace(DISCORD_TOKEN, "***")
    if TG_BOT_TOKEN:
        text = text.replace(TG_BOT_TOKEN, "***")
    if TG_CHAT_ID:
        text = text.replace(TG_CHAT_ID, "***")
    for val in _SENSITIVE_VALUES:
        if val in text:
            text = text.replace(val, "***")
    for sid, idx in _SERVER_INDEX.items():
        if sid in text:
            text = text.replace(sid, f"服务器#{idx}")
    text = re.sub(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.)\d{1,3}\b", r"\1xx", text)
    text = re.sub(r"connect\.sid=[^;\s]+", "connect.sid=***", text)
    return text


def log_info(msg: str):  print(f"[INFO] {_mask(msg)}")
def log_warn(msg: str):  print(f"[WARN] {_mask(msg)}")
def log_error(msg: str): print(f"[ERROR] {_mask(msg)}")

def parse_remaining(text: str) -> str | None:
    if not text:
        return None
    d = re.search(r"(\d+(?:\.\d+)?)\s*day", text, re.I)
    h = re.search(r"(\d+(?:\.\d+)?)\s*hour", text, re.I)
    days_raw  = float(d.group(1)) if d else 0.0
    hours_raw = float(h.group(1)) if h else 0.0
    extra_hours = (days_raw - int(days_raw)) * 24
    total_hours = hours_raw + extra_hours
    final_days  = int(days_raw)
    final_hours = int(total_hours)
    final_mins  = int(round((total_hours - final_hours) * 60))
    parts = []
    if final_days > 0:
        parts.append(f"{final_days}天")
    if final_hours > 0 or final_days > 0:
        parts.append(f"{final_hours}时")
    parts.append(f"{final_mins}分")
    return "".join(parts) if parts else None


def remaining_total_days(text: str) -> float | None:
    if not text:
        return None
    d = re.search(r"(\d+(?:\.\d+)?)\s*day", text, re.I)
    h = re.search(r"(\d+(?:\.\d+)?)\s*hour", text, re.I)
    days  = float(d.group(1)) if d else 0.0
    hours = float(h.group(1)) if h else 0.0
    return days + hours / 24.0

def extract_email(page) -> str | None:
    try:
        log_info("打开 Settings 页面获取邮箱...")
        page.goto(f"{BASE_URL}/settings", wait_until="networkidle")
        page.wait_for_timeout(3000)
        email = page.evaluate(r"""() => {
            const labels = document.querySelectorAll('p');
            for (const label of labels) {
                if (label.textContent.trim().toLowerCase().includes('email address')) {
                    const next = label.nextElementSibling;
                    if (next) {
                        const text = next.textContent.trim();
                        if (text.includes('@')) return text;
                    }
                }
            }
            const body = document.body.innerText;
            const m = body.match(/[\w.+-]+@[\w.-]+\.\w+/);
            return m ? m[0] : null;
        }""")
        if email:
            _register_sensitive(email)
            log_info(f"邮箱获取成功: {email}")
            return email
        log_warn("Settings 页面未找到邮箱")
        return None
    except Exception as e:
        log_warn(f"获取邮箱失败: {e}")
        return None

def send_tg(caption: str, image_bytes: bytes | None = None):
    if not TG_CHAT_ID or not TG_BOT_TOKEN:
        log_warn("TG 未配置，跳过推送")
        return
    try:
        if image_bytes:
            boundary = f"----Boundary{abs(hash(caption))}"
            body_parts = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{TG_CHAT_ID}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"{caption}\r\n"
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="photo"; filename="s.png"\r\n'
                f"Content-Type: image/png\r\n\r\n"
            ).encode() + image_bytes + f"\r\n--{boundary}--\r\n".encode()
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                data=body_parts,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
        else:
            req = Request(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data=json.dumps({"chat_id": TG_CHAT_ID, "text": caption}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        with urlopen(req, timeout=30) as resp:
            log_info("TG 推送成功" if resp.status == 200 else f"TG 推送失败: HTTP {resp.status}")
    except Exception as e:
        log_warn(f"TG 推送异常: {e}")

def take_screenshot(page, name: str) -> bytes | None:
    try:
        page.set_viewport_size({"width": VIEWPORT_W, "height": VIEWPORT_H})
        page.wait_for_timeout(500)
        path = SCREENSHOT_DIR / f"{name}.png"
        page.screenshot(path=str(path), full_page=False)
        log_info(f"截图已保存: {path}")
        return path.read_bytes()
    except Exception as e:
        log_warn(f"截图失败: {e}")
        return None


def merge_screenshots(browser, buffers: list[bytes]) -> bytes | None:
    if not buffers:
        return None
    log_info("合并截图...")
    pg = browser.new_page(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
    try:
        imgs = "".join(
            f'<img src="data:image/png;base64,{base64.b64encode(b).decode()}" '
            f'style="width:100%;border-radius:8px;border:2px solid #202225;'
            f'box-shadow:0 4px 6px rgba(0,0,0,.3);" />'
            for b in buffers
        )
        pg.set_content(
            f'<body style="margin:0;padding:15px;background:#2f3136;'
            f'display:flex;flex-direction:column;gap:15px;">{imgs}</body>'
        )
        pg.wait_for_timeout(500)
        return pg.screenshot(full_page=True)
    except Exception as e:
        log_warn(f"截图合并失败: {e}")
        return None
    finally:
        pg.close()

def check_site_down(page) -> bool:
    """Detect FreezeHost 'CONNECTION TO THE MANAGEMENT SERVICES LOST' or similar outage screens."""
    try:
        return page.evaluate("""() => {
            const body = document.body ? document.body.innerText : '';
            if (body.includes('CONNECTION TO THE MANAGEMENT SERVICES LOST')) return true;
            if (body.includes('Retrying in') && body.includes('Retry Now')) return true;
            if (document.querySelector('button:has-text("Retry Now")')) return true;
            return false;
        }""")
    except Exception:
        return False


# 多组选择器: FreezeHost 可能改版, 不再只用 span.text-lg
LOGIN_BUTTON_SELECTORS = [
    'span.text-lg:has-text("Login with Discord")',
    'span:has-text("Login with Discord")',
    'button:has-text("Login with Discord")',
    'a:has-text("Login with Discord")',
    'div:has-text("Login with Discord")',
    '[class*="login"]:has-text("Discord")',
    'span:has-text("Discord でログイン")',  # 日语
    'span:has-text("Discordでログイン")',
    'span:has-text("以 Discord 登录")',     # 中文
    'span:has-text("使用 Discord 登录")',
]


def find_login_button(page, timeout_ms: int = 2000):
    """尝试多组选择器查找登录按钮, 找到返回 locator, 否则 None"""
    for sel in LOGIN_BUTTON_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout_ms):
                log_info(f"找到登录按钮, 选择器: {sel}")
                return loc
        except Exception:
            continue
    return None


def is_already_logged_in(page) -> bool:
    """检测是否已登录态 (跳过了登录页直接到 dashboard)"""
    try:
        url = page.url or ""
        if "/dashboard" in url or "/server-console" in url or "/callback" in url:
            return True
        # 检查页面是否有登出按钮 / 服务器列表等登录后才会出现的元素
        for sel in ['a[href*="dashboard"]', 'a[href*="server-console"]',
                    'button:has-text("Logout")', 'button:has-text("Sign out")']:
            try:
                if page.locator(sel).first.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def wait_for_site_ready(page) -> bool:
    """Try loading FreezeHost up to MAX_SITE_RETRIES times, handling outage screens.
    Returns True if site became available AND login button is visible OR already logged in."""
    for attempt in range(1, MAX_SITE_RETRIES + 1):
        log_info(f"加载 FreezeHost 首页 (尝试 {attempt}/{MAX_SITE_RETRIES})...")
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT)
        except PlaywrightTimeout:
            log_warn(f"首页加载超时 (尝试 {attempt})")
            if attempt < MAX_SITE_RETRIES:
                page.wait_for_timeout(RETRY_WAIT)
            continue

        # SPA 渲染需要时间, 5s 比 3s 更稳妥
        page.wait_for_timeout(5000)

        if check_site_down(page):
            log_warn(f"FreezeHost 后端服务不可用 (尝试 {attempt})")
            take_screenshot(page, f"site-down-{attempt}")

            # Try clicking the "Retry Now" button on the page itself
            try:
                retry_btn = page.locator('button:has-text("Retry Now")')
                if retry_btn.is_visible():
                    log_info("点击页面 Retry Now 按钮...")
                    retry_btn.click()
                    page.wait_for_timeout(10_000)
                    if not check_site_down(page):
                        log_info("站点恢复正常")
                        # 继续往下检查登录按钮
                    else:
                        if attempt < MAX_SITE_RETRIES:
                            page.wait_for_timeout(RETRY_WAIT)
                        continue
            except Exception:
                pass

            if attempt < MAX_SITE_RETRIES:
                log_info(f"等待 {RETRY_WAIT // 1000} 秒后重试...")
                page.wait_for_timeout(RETRY_WAIT)
            continue

        # 1) 检测是否已登录 (跳过登录页)
        if is_already_logged_in(page):
            log_info(f"检测到已登录状态, 当前 URL: {page.url}")
            return True

        # 2) 多组选择器查找登录按钮
        btn = find_login_button(page, timeout_ms=2000)
        if btn:
            log_info("首页加载正常, 登录按钮可见")
            return True

        # 3) 还没找到 -> 截图 + 保存 HTML 供排查, 然后重试
        log_warn(f"首页已加载但未找到登录按钮 (尝试 {attempt})")
        take_screenshot(page, f"no-login-btn-{attempt}")
        try:
            html_path = SCREENSHOT_DIR / f"no-login-btn-{attempt}.html"
            html_path.write_text(page.content(), encoding="utf-8")
            log_info(f"页面 HTML 已保存: {html_path}")
        except Exception as e:
            log_warn(f"保存 HTML 失败: {e}")

        if attempt < MAX_SITE_RETRIES:
            log_info(f"等待 {RETRY_WAIT // 1000} 秒后重试...")
            page.wait_for_timeout(RETRY_WAIT)

    return False


def handle_oauth_page(page):
    log_info("进入 OAuth 授权页处理")
    page.wait_for_timeout(2000)

    for _ in range(20):
        if "discord.com" not in page.url:
            return
        btn_text = ""
        try:
            for sel in ['button[type="submit"]', 'div[class*="footer"] button', 'button[class*="primary"]']:
                btn = page.locator(sel).last
                if btn.is_visible():
                    btn_text = btn.inner_text().strip().lower()
                    break
        except Exception:
            pass
        if "authorize" in btn_text and "scroll" not in btn_text:
            break
        page.evaluate("""() => {
            const sels = ['[class*="scroller"]','[class*="oauth2"]','[class*="permissionList"]',
                '[class*="content"] [class*="scroll"]','[class*="listScroller"]',
                'div[class*="modal"] div[style*="overflow"]','div[class*="root"] div[style*="overflow"]'];
            let scrolled = false;
            for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                    const s = getComputedStyle(el);
                    if (el.scrollHeight > el.clientHeight &&
                        ['auto','scroll'].some(v => s.overflowY === v || s.overflow === v))
                        { el.scrollTop = el.scrollHeight; scrolled = true; }
                }
            }
            if (!scrolled) document.querySelectorAll('div').forEach(el => {
                if (el.scrollHeight > el.clientHeight + 10) {
                    const s = getComputedStyle(el);
                    if (['auto','scroll','hidden'].includes(s.overflowY)) el.scrollTop = el.scrollHeight;
                }
            });
            scrollTo(0, document.body.scrollHeight);
        }""")
        page.wait_for_timeout(800)

    for _ in range(10):
        if "discord.com" not in page.url:
            return
        for sel in ['button:has-text("Authorize")','button:has-text("授权")',
                    'button[type="submit"]','div[class*="footer"] button','button[class*="primary"]']:
            try:
                btn = page.locator(sel).last
                if not btn.is_visible():
                    continue
                text = btn.inner_text().strip()
                if any(k in text.lower() for k in ("取消","cancel","deny")):
                    continue
                if "scroll" in text.lower():
                    page.evaluate("""() => {
                        document.querySelectorAll('div').forEach(el => {
                            if (el.scrollHeight > el.clientHeight + 5) el.scrollTop = el.scrollHeight;
                        }); scrollTo(0, document.body.scrollHeight);
                    }""")
                    page.wait_for_timeout(1000)
                    break
                if btn.is_disabled():
                    page.wait_for_timeout(1000)
                    break
                btn.click()
                page.wait_for_timeout(2000)
                if "discord.com" not in page.url:
                    return
                break
            except Exception:
                continue
        page.wait_for_timeout(1500)

def discover_server_ids(page) -> list[str]:
    for attempt in range(3):
        captured: set[str] = set()

        def on_req(req):
            m = re.search(r"/api/server(?:resources|network|subdomain)\?id=([a-f0-9]+)", req.url, re.I)
            if m:
                captured.add(m.group(1))

        page.on("request", on_req)
        if attempt == 0:
            log_info("加载 Dashboard 发现服务器...")
            page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle")
        else:
            log_info(f"第 {attempt+1} 次重试...")
            page.reload(wait_until="networkidle")

        page.wait_for_timeout(5000)
        page.remove_listener("request", on_req)

        js_ids = page.evaluate(r"""() => {
            const ids = [];
            if (typeof serverData !== 'undefined' && Array.isArray(serverData))
                serverData.forEach(s => { if (s.identifier) ids.push(s.identifier); });
            if (!ids.length) document.querySelectorAll('script:not([src])').forEach(sc => {
                for (const m of sc.textContent.matchAll(/identifier:\s*["']([a-f0-9]{6,})["']/gi))
                    ids.push(m[1]);
            });
            return ids;
        }""")

        all_ids = set(js_ids or []) | (captured if not js_ids else set())
        for sid in sorted(all_ids):
            _server_label(sid)
            _register_sensitive(sid)

        if all_ids:
            log_info(f"发现 {len(all_ids)} 台服务器")
            return sorted(all_ids)

        log_warn(f"第 {attempt+1} 次未发现服务器")
        take_screenshot(page, f"dashboard-empty-{attempt+1}")
        if attempt < 2:
            page.wait_for_timeout(3000)

    return []

def process_server(page, server_id: str) -> dict:
    tag = _server_label(server_id)
    server_url = f"{BASE_URL}/server-console?id={server_id}"
    result = dict(server_id=server_id, status="unknown", before=None, after=None,
                  emoji="❓", status_label="未知", detail="")

    log_info(f"[{server_id}] 开始处理")
    try:
        page.goto(server_url, wait_until="networkidle")
        page.wait_for_timeout(3000)

        status_text = page.evaluate("""() => {
            const el = document.getElementById('renewal-status-console');
            return el ? el.innerText.trim() : null;
        }""")
        log_info(f"[{server_id}] 续期状态: {status_text or '(空)'}")

        remaining_before = parse_remaining(status_text)
        total_days = remaining_total_days(status_text)
        result["before"] = remaining_before

        if total_days is not None and total_days > 7:
            log_info(f"[{server_id}] 剩余 {total_days:.1f} 天，无需续期")
            result.update(status="cooldown", emoji="⏳", status_label="冷却期",
                          detail=remaining_before or f"{total_days:.1f}天")
            return result

        # ── 查找续期链接 ─────────────────────────────────
        renew_href = page.evaluate("""() => {
            const rl = document.getElementById('renew-link-modal');
            if (rl) { const h = rl.getAttribute('href'); if (h && h !== '#') return {href:h, text:rl.innerText.trim()}; }
            for (const a of document.querySelectorAll('a[href*="renew"]')) {
                const h = a.getAttribute('href');
                if (h && h.includes('renew') && h !== '#') return {href:h, text:a.innerText.trim()};
            }
            return null;
        }""")

        if not (renew_href and renew_href.get("href")):
            # 尝试点击外链图标
            page.evaluate("""() => {
                const icon = document.querySelector('i.fa-external-link-alt');
                if (icon) { (icon.closest('button') || icon.parentElement || icon).click(); return; }
                if (typeof reviewAction === 'function') reviewAction('done');
            }""")
            page.wait_for_timeout(2000)

            renew_href = page.evaluate("""() => {
                const rl = document.getElementById('renew-link-modal');
                if (rl) { const h = rl.getAttribute('href'); if (h && h !== '#') return {href:h, text:rl.innerText.trim()}; }
                return null;
            }""")

        if not (renew_href and renew_href.get("href")):
            renew_href = page.evaluate(r"""() => {
                const m = document.body.innerHTML.match(/href=["']((?:\.\.)?\/renew\?id=[a-f0-9]+)["']/i);
                return m ? {href:m[1], text:'html-extract'} : null;
            }""")

        if not (renew_href and renew_href.get("href")):
            raise RuntimeError("未找到续期链接")

        btn_text = renew_href.get("text", "")
        href = renew_href["href"]

        if btn_text and "renew instance" not in btn_text.lower():
            if not (total_days is not None and total_days <= 7):
                result.update(status="tooearly", emoji="⏳", status_label="冷却期",
                              detail=remaining_before or btn_text)
                return result

        # ── 执行续期 ─────────────────────────────────────
        page.goto(urljoin(page.url, href), wait_until="domcontentloaded")
        try:
            page.wait_for_url(lambda u: "/dashboard" in u or "/server-console" in u, timeout=30000)
        except PlaywrightTimeout:
            pass

        url = page.url
        if "success=RENEWED" in url:
            log_info(f"[{server_id}] 续期成功！")
            try:
                page.goto(server_url, wait_until="networkidle")
                page.wait_for_timeout(3000)
                after_text = page.evaluate("""() => {
                    const el = document.getElementById('renewal-status-console');
                    return el ? el.innerText.trim() : null;
                }""")
                result["after"] = parse_remaining(after_text)
            except Exception:
                pass
            result.update(status="renewed", emoji="✅", status_label="续期成功",
                          detail=f"{result['before'] or '?'} → {result['after'] or '?'}")
        elif "err=CANNOTAFFORDRENEWAL" in url:
            result.update(status="broke", emoji="⚠️", status_label="余额不足",
                          detail=remaining_before or "")
        elif "err=TOOEARLY" in url:
            result.update(status="tooearly", emoji="⏳", status_label="冷却期",
                          detail=remaining_before or "")
        else:
            result.update(status="unknown", emoji="❓", status_label="结果未知")

    except Exception as e:
        log_error(f"[{server_id}] 异常: {e}")
        result.update(status="error", emoji="❌", status_label="脚本异常",
                      detail=str(e)[:80])

    return result

#  主流程
def run():
    if not DISCORD_TOKEN:
        raise RuntimeError("缺少 FREEZEHOST_DISCORD_TOKEN")

    log_info("启动浏览器 (WARP 系统级代理)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": VIEWPORT_W, "height": VIEWPORT_H})
        page.set_default_timeout(TIMEOUT)
        log_info("浏览器就绪")

        display_name = "未知用户"

        try:
            # ── 出口 IP ───────────────────────────────────
            log_info("验证出口 IP...")
            try:
                ip = json.loads(page.goto("https://api.ipify.org?format=json",
                                          wait_until="domcontentloaded").text()).get("ip", "?")
                log_info(f"出口 IP: {ip}")
            except Exception:
                log_warn("IP 验证超时")

            # ── 检测站点可用性（带重试） ─────────────────
            log_info("打开 FreezeHost 登录页")
            if not wait_for_site_ready(page):
                buf = take_screenshot(page, "site-down-final")
                msg = (
                    f"用户：{display_name}\n"
                    f"🔌 FreezeHost 站点不可用 / 未找到登录按钮\n"
                    f"已重试 {MAX_SITE_RETRIES} 次仍失败\n"
                    f"可能原因: 站点改版 / 网络问题 / Cloudflare 拦截\n"
                    f"请手动访问 {BASE_URL} 检查\n\n"
                    f"FreezeHost Auto Renew"
                )
                send_tg(msg, buf)
                log_warn("站点不可用，本次跳过续期")
                return   # Exit gracefully — not a script error

            # ── 已登录态直接跳过 OAuth ───────────────────
            skipped_oauth = False
            if is_already_logged_in(page):
                log_info(f"已是登录态, 跳过 OAuth 流程, 当前 URL: {page.url}")
                skipped_oauth = True
                # 跳到 dashboard 发现服务器
                if "/dashboard" not in page.url:
                    page.goto(f"{BASE_URL}/dashboard", wait_until="networkidle")
                    page.wait_for_timeout(3000)
            else:
                # ── 登录 ─────────────────────────────────────
                # 用 find_login_button 找到的 locator 点击, 不再硬编码选择器
                btn = find_login_button(page, timeout_ms=5000)
                if not btn:
                    buf = take_screenshot(page, "login-btn-not-found")
                    try:
                        html_path = SCREENSHOT_DIR / "login-btn-not-found.html"
                        html_path.write_text(page.content(), encoding="utf-8")
                    except Exception:
                        pass
                    send_tg(
                        f"用户：{display_name}\n"
                        f"❌ 找不到 Login with Discord 按钮\n"
                        f"FreezeHost 页面可能改版, 请检查截图\n"
                        f"当前 URL: {page.url}\n\n"
                        f"FreezeHost Auto Renew",
                        buf,
                    )
                    raise RuntimeError("找不到登录按钮, FreezeHost 可能改版")

                btn.click()
                log_info("已点击登录按钮")

                # 等待可能出现的「服务条款确认」对话框
                try:
                    confirm_btn = page.locator("button#confirm-login")
                    confirm_btn.wait_for(state="visible", timeout=5000)
                    confirm_btn.click()
                    log_info("已接受服务条款")
                except PlaywrightTimeout:
                    log_info("未出现服务条款确认对话框, 继续")
                except Exception as e:
                    log_warn(f"服务条款确认异常: {e}")

                page.wait_for_url(re.compile(r"discord\.com"), timeout=15000)
                log_info("已到达 Discord")

                # ── 注入 Token ────────────────────────────────
                # 旧方案：用 iframe.contentWindow.localStorage 注入，
                #   但 Discord 页面的 CSP / sandbox 会让 contentWindow 为 null，导致抛错。
                # 新方案：add_init_script 在每个页面加载前注入 localStorage，
                #   不依赖 iframe，绕过 CSP，且对所有后续跳转也生效。
                try:
                    page.add_init_script(f"""() => {{
                        try {{
                            localStorage.setItem('token', JSON.stringify({json.dumps(DISCORD_TOKEN)}));
                        }} catch (e) {{}}
                    }}""")
                    log_info("Token init script 已注册")
                except Exception as e:
                    log_warn(f"add_init_script 注册失败，回退到 evaluate 注入: {e}")
                    try:
                        page.evaluate("""(token) => {
                            try { localStorage.setItem('token', '"'+token+'"'); } catch(e) {}
                        }""", DISCORD_TOKEN)
                    except Exception as e2:
                        log_error(f"evaluate 注入也失败: {e2}")

                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(3000)

                if re.search(r"discord\.com/login", page.url):
                    buf = take_screenshot(page, "token-failed")
                    send_tg(
                        f"用户：{display_name}\n"
                        f"❌ Discord Token 已失效\n"
                        f"请重新获取 Token 并更新 GitHub Secrets:\n"
                        f"  FREEZEHOST_DISCORD_TOKEN_{int(os.environ.get('TOKEN_NUM', '1'))}\n"
                        f"教程: 浏览器 F12 → Network → 任意请求 → Headers → Authorization\n"
                        f"\nFreezeHost Auto Renew",
                        buf,
                    )
                    raise RuntimeError("Token 登录失败（Token 已失效，需更新 GitHub Secrets）")

                log_info("Token 注入成功")

                # ── OAuth ─────────────────────────────────────
                try:
                    page.wait_for_url(re.compile(r"discord\.com/oauth2/authorize"), timeout=6000)
                    page.wait_for_timeout(2000)
                    if "discord.com" in page.url:
                        handle_oauth_page(page)
                    if "discord.com" in page.url:
                        try:
                            page.wait_for_url(re.compile(r"free\.freezehost\.pro"), timeout=20000)
                        except PlaywrightTimeout:
                            buf = take_screenshot(page, "oauth-stuck")
                            send_tg(
                                f"用户：{display_name}\n"
                                f"❌ OAuth 授权页卡住，未能跳转回 FreezeHost\n"
                                f"可能原因: Discord OAuth 页面 DOM 改版 / Token 权限不足 / 网络问题\n"
                                f"建议: 手动登录一次 https://free.freezehost.pro 完成授权后重试\n"
                                f"\nFreezeHost Auto Renew",
                                buf,
                            )
                            raise RuntimeError("OAuth 未跳转（已发截图供人工排查）")
                except PlaywrightTimeout:
                    if "discord.com" in page.url:
                        buf = take_screenshot(page, "oauth-timeout")
                        send_tg(
                            f"用户：{display_name}\n"
                            f"❌ OAuth 等待 authorize 页超时\n"
                            f"当前 URL: {page.url}\n"
                            f"\nFreezeHost Auto Renew",
                            buf,
                        )
                        raise RuntimeError("OAuth 超时（已发截图）")

            # ── Dashboard ─────────────────────────────────
            if not skipped_oauth:
                try:
                    page.wait_for_url(lambda u: "/callback" in u or "/dashboard" in u, timeout=10000)
                except PlaywrightTimeout:
                    pass
                if "/callback" in page.url:
                    page.wait_for_url(re.compile(r"/dashboard"), timeout=15000)
                if "/dashboard" not in page.url:
                    take_screenshot(page, "not-dashboard")
                    raise RuntimeError("未到达 Dashboard")

            log_info("登录成功")

            # ── 邮箱（唯一显示名） ───────────────────────
            email = extract_email(page)
            if email:
                display_name = email
            else:
                log_warn("邮箱获取失败，TG 将显示「未知用户」")

            # ── 发现服务器 ────────────────────────────────
            server_ids = discover_server_ids(page)
            if not server_ids:
                buf = take_screenshot(page, "no-servers")
                send_tg(f"用户：{display_name}\n⚠️ 未发现服务器\n\nFreezeHost Auto Renew", buf)
                return

            # ── 逐台处理 ─────────────────────────────────
            results, screenshots = [], []
            for sid in server_ids:
                log_info("=" * 50)
                res = process_server(page, sid)
                results.append(res)
                buf = take_screenshot(page, f"server-{_SERVER_INDEX.get(sid, 0)}")
                if buf:
                    screenshots.append(buf)

            # ── 合并截图 ─────────────────────────────────
            final_img = (screenshots[0] if len(screenshots) == 1
                         else merge_screenshots(browser, screenshots) if screenshots
                         else None)

            # ── TG 推送（完整信息） ──────────────────────
            lines = []
            for r in results:
                s = f"服务器: {r['server_id']} | {r['emoji']}{r['status_label']}"
                if r["detail"]:
                    s += f" {r['detail']}"
                lines.append(s)

            send_tg("\n".join([f"用户：{display_name}", *lines, "", "FreezeHost Auto Renew"]), final_img)
            log_info("所有服务器处理完毕")

        except Exception as e:
            buf = take_screenshot(page, "fatal-error")
            send_tg(f"用户：{display_name}\n❌ 异常: {e}\n\nFreezeHost Auto Renew", buf)
            raise
        finally:
            browser.close()


if __name__ == "__main__":
    try:
        run()
        log_info("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
        sys.exit(1)
