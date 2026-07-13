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

# 代理配置 (复用 katabump 的 sing-box 方案)
# IS_PROXY=true 时 PROXY_SERVER 形如 socks5://127.0.0.1:1080
IS_PROXY      = os.environ.get("IS_PROXY", "false").lower() == "true"
PROXY_SERVER  = os.environ.get("PROXY_SERVER", "").strip()

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
    """Detect FreezeHost 'CONNECTION TO THE MANAGEMENT SERVICES LOST' or similar outage screens.
    兼容大小写: HTML 里是 'Connection to the Management Services Lost' (首字母大写)
    """
    try:
        return page.evaluate("""() => {
            const body = document.body ? document.body.innerText : '';
            const bodyLower = body.toLowerCase();
            // 大小写不敏感匹配
            if (bodyLower.includes('connection to the management services lost')) return true;
            if (bodyLower.includes('retrying in') && bodyLower.includes('retry now')) return true;
            if (bodyLower.includes('service unavailable')) return true;
            // 检查 Retry Now 按钮 (大小写不敏感)
            const retryBtn = document.querySelector('button');
            if (retryBtn && retryBtn.innerText && retryBtn.innerText.toLowerCase().includes('retry now')) return true;
            // 检查 OOPS 标题 (FreezeHost 错误页特征)
            if (bodyLower.includes('oops') && bodyLower.includes('retry')) return true;
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
            log_warn(f"FreezeHost 后端服务不可用 (OOPS / Connection Lost, 尝试 {attempt})")
            take_screenshot(page, f"site-down-{attempt}")

            # 主动点 Retry Now 按钮触发重试 (FreezeHost 错误页有这个按钮)
            try:
                # 多组选择器找 Retry Now
                for sel in ['button:has-text("Retry Now")', 'button:has-text("Retry")',
                           'button[onclick*="reload"]', 'button:has-text("重试")']:
                    try:
                        retry_btn = page.locator(sel).first
                        if retry_btn.is_visible(timeout=2000):
                            log_info(f"点击 Retry Now 按钮 (选择器: {sel})...")
                            retry_btn.click()
                            page.wait_for_timeout(8000)  # 等待重试后页面加载
                            if not check_site_down(page):
                                log_info("站点恢复正常")
                                break  # 跳出 for, 继续往下检查登录按钮
                            else:
                                log_warn("点 Retry 后还是 OOPS, 继续重试")
                                break  # 跳出 for, 继续外层 while
                    except Exception:
                        continue
            except Exception as e:
                log_warn(f"点 Retry 异常: {e}")

            # 主动 reload 也试一下
            if check_site_down(page):
                try:
                    log_info("尝试 page.reload()...")
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(5000)
                    if not check_site_down(page):
                        log_info("reload 后站点恢复正常")
                        # 继续往下检查登录按钮
                    else:
                        if attempt < MAX_SITE_RETRIES:
                            log_info(f"等待 {RETRY_WAIT // 1000} 秒后重试...")
                            page.wait_for_timeout(RETRY_WAIT)
                        continue
                except Exception as e:
                    log_warn(f"reload 异常: {e}")

            if check_site_down(page):
                if attempt < MAX_SITE_RETRIES:
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

def safe_goto(page, url, timeout=30000, wait_until="domcontentloaded"):
    """安全的 page.goto 包装: 容错处理 ERR_ABORTED 和导航中断
    FreezeHost 改版后, 直接 goto /dashboard 可能被前端重定向中断
    改用 domcontentloaded 而不是 networkidle, 并捕获异常继续执行
    """
    try:
        page.goto(url, wait_until=wait_until, timeout=timeout)
        return True
    except PlaywrightTimeout:
        log_warn(f"goto 超时: {url}")
        return False
    except Exception as e:
        err_str = str(e)
        # ERR_ABORTED 通常是前端重定向导致的, 不算真错误
        if "ERR_ABORTED" in err_str or "aborted" in err_str.lower():
            log_info(f"goto 被前端重定向中断 (ERR_ABORTED, 通常正常): {url}")
            page.wait_for_timeout(3000)  # 等重定向完成
            return True
        log_warn(f"goto 异常: {url} - {e}")
        return False


def discover_server_ids(page) -> list[str]:
    """发现服务器列表 - 兼容 FreezeHost 改版
    之前用 page.goto(BASE_URL + '/dashboard', wait_until='networkidle')
    FreezeHost 改版后 /dashboard 可能重定向到 / 或其他路径, networkidle 永远等不到
    改用 safe_goto + 多路径兜底
    """
    # 可能的 dashboard 路径, 按优先级尝试
    dashboard_paths = [
        f"{BASE_URL}/dashboard",
        f"{BASE_URL}/",
        f"{BASE_URL}/servers",
        f"{BASE_URL}/home",
    ]

    for attempt in range(3):
        captured: set[str] = set()

        def on_req(req):
            m = re.search(r"/api/server(?:resources|network|subdomain)\?id=([a-f0-9]+)", req.url, re.I)
            if m:
                captured.add(m.group(1))

        page.on("request", on_req)
        if attempt == 0:
            log_info("加载 Dashboard 发现服务器...")
            # 尝试多个可能的 dashboard 路径
            for path in dashboard_paths:
                log_info(f"尝试打开: {path}")
                if safe_goto(page, path, timeout=20000):
                    page.wait_for_timeout(3000)
                    current_url = page.url
                    log_info(f"当前 URL: {current_url}")
                    # 如果没被重定向到 login, 说明路径有效
                    if "/login" not in current_url and "discord.com" not in current_url:
                        log_info(f"路径有效: {path}")
                        break
        else:
            log_info(f"第 {attempt+1} 次重试 (reload)...")
            try:
                page.reload(wait_until="domcontentloaded", timeout=20000)
            except Exception as e:
                log_warn(f"reload 异常: {e}")
                # reload 失败就重新 goto
                safe_goto(page, f"{BASE_URL}/", timeout=20000)

        page.wait_for_timeout(5000)
        page.remove_listener("request", on_req)

        # 多种方式提取服务器 ID (兼容 FreezeHost 改版)
        js_ids = page.evaluate(r"""() => {
            const ids = [];

            // 方式1: 全局 serverData 变量
            try {
                if (typeof serverData !== 'undefined' && Array.isArray(serverData))
                    serverData.forEach(s => { if (s.identifier) ids.push(s.identifier); });
            } catch(e) {}

            // 方式2: 从 script 标签内容提取 identifier
            if (!ids.length) {
                document.querySelectorAll('script:not([src])').forEach(sc => {
                    try {
                        for (const m of sc.textContent.matchAll(/identifier:\s*["']([a-f0-9]{6,})["']/gi))
                            ids.push(m[1]);
                    } catch(e) {}
                });
            }

            // 方式3: 从 a[href*="server-console"] 链接提取
            if (!ids.length) {
                document.querySelectorAll('a[href*="server-console?id="]').forEach(a => {
                    const m = a.href.match(/id=([a-f0-9]{6,})/i);
                    if (m) ids.push(m[1]);
                });
            }

            // 方式4: 从 a[href*="server/"] 链接提取
            if (!ids.length) {
                document.querySelectorAll('a[href*="/server/"]').forEach(a => {
                    const m = a.href.match(/\/server\/([a-f0-9]{6,})/i);
                    if (m) ids.push(m[1]);
                });
            }

            // 方式5: 从按钮 data-id / data-server-id 属性提取
            if (!ids.length) {
                document.querySelectorAll('[data-server-id], [data-id]').forEach(el => {
                    const v = el.getAttribute('data-server-id') || el.getAttribute('data-id');
                    if (v && /^[a-f0-9]{6,}$/i.test(v)) ids.push(v);
                });
            }

            return [...new Set(ids)];
        }""")

        all_ids = set(js_ids or []) | (captured if not js_ids else set())
        for sid in sorted(all_ids):
            _server_label(sid)
            _register_sensitive(sid)

        if all_ids:
            log_info(f"发现 {len(all_ids)} 台服务器")
            return sorted(all_ids)

        log_warn(f"第 {attempt+1} 次未发现服务器, 当前 URL: {page.url}")
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
        safe_goto(page, server_url, timeout=30000)
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
                safe_goto(page, server_url, timeout=30000)
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

    # 启动浏览器 (可选 sing-box 代理, 复用 katabump 方案)
    # 关键: 用 headless=False 绕过 FreezeHost 的 headless 检测
    # 在 GitHub Actions 里配合 xvfb-run 使用, 提供虚拟显示器
    # 重要: 即便 headless=False, Playwright 在某些环境下仍会发 HeadlessChrome UA
    # 必须强制覆盖 UA + 在 init_script 里硬改 navigator.userAgent
    REAL_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    launch_kwargs = {
        "headless": False,  # 必须非 headless, 否则被 FreezeHost 检测拉黑
        "channel": "chrome",  # 用系统 chrome 而不是 chromium (避免 HeadlessChrome 标志)
        "args": [
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",  # 隐藏 navigator.webdriver
            "--disable-dev-shm-usage",
            "--window-size=1280,753",
            f"--user-agent={REAL_UA}",  # 启动参数也指定 UA
        ],
    }
    if IS_PROXY and PROXY_SERVER:
        # Playwright 接受形如 "socks5://127.0.0.1:1080" 或 "http://127.0.0.1:1081"
        launch_kwargs["proxy"] = {"server": PROXY_SERVER}
        log_info(f"启动浏览器 (代理: {PROXY_SERVER}, 非 headless 反检测)")
    else:
        log_info("启动浏览器 (直连, 无代理 - 可能被 FreezeHost 拉黑)")

    # 用 storage_state 在启动时直接写入 Discord Token 到 localStorage
    # 这是 Playwright 官方推荐方式, 不依赖页面加载时机
    # 关键: origins 必须是 https://discord.com, name 是 'token', value 是带引号的 JSON 字符串
    storage_state = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://discord.com",
                "localStorage": [
                    {"name": "token", "value": json.dumps(DISCORD_TOKEN)}
                ]
            }
        ]
    }

    with sync_playwright() as pw:
        # 尝试用 chrome channel, 失败回退到 chromium
        try:
            browser = pw.chromium.launch(**launch_kwargs)
        except Exception as e:
            log_warn(f"用 chrome channel 启动失败: {e}, 回退到 chromium")
            launch_kwargs.pop("channel", None)
            browser = pw.chromium.launch(**launch_kwargs)
        # 用 storage_state 创建 context, 这样 discord.com 的 localStorage 会预写入 token
        context = browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            storage_state=storage_state,
            user_agent=REAL_UA,  # 强制 UA
            locale="en-US",
            timezone_id="America/New_York",
        )
        # 注入反检测脚本: 强制覆盖 UA + 隐藏 webdriver
        # 关键: FreezeHost 检测 userAgent_headless, 必须把 HeadlessChrome 改成 Chrome
        context.add_init_script(f"""
            // 1. 强制覆盖 navigator.userAgent (最关键, FreezeHead 检测 userAgent_headless)
            try {{
                Object.defineProperty(navigator, 'userAgent', {{
                    get: () => '{REAL_UA}',
                    configurable: true
                }});
            }} catch(e) {{}}

            // 2. 强制覆盖 navigator.appVersion (与 UA 配套, 也会暴露 HeadlessChrome)
            try {{
                Object.defineProperty(navigator, 'appVersion', {{
                    get: () => '5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
                    configurable: true
                }});
            }} catch(e) {{}}

            // 3. 隐藏 navigator.webdriver (FreezeHost 必检测项)
            try {{
                Object.defineProperty(navigator, 'webdriver', {{
                    get: () => undefined,
                    configurable: true
                }});
            }} catch(e) {{}}

            // 4. 伪造 navigator.plugins (headless 默认 0)
            try {{
                Object.defineProperty(navigator, 'plugins', {{
                    get: () => {{
                        const arr = [
                            {{ name: 'Chrome PDF Plugin' }},
                            {{ name: 'Chrome PDF Viewer' }},
                            {{ name: 'Native Client' }}
                        ];
                        arr.refresh = () => {{}};
                        arr.item = (i) => arr[i] || null;
                        arr.namedItem = (n) => arr.find(p => p.name === n) || null;
                        return arr;
                    }},
                    configurable: true
                }});
            }} catch(e) {{}}

            // 5. 伪造 navigator.languages
            try {{
                Object.defineProperty(navigator, 'languages', {{
                    get: () => ['en-US', 'en'],
                    configurable: true
                }});
            }} catch(e) {{}}

            // 6. 修复 permissions API (FreezeHost 检测 permissions_mismatch)
            try {{
                const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
                window.navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({{ state: Notification.permission }})
                        : origQuery(parameters);
            }} catch(e) {{}}

            // 7. window.chrome (headless 默认没有)
            try {{
                if (!window.chrome) {{
                    window.chrome = {{ runtime: {{}} }};
                }}
            }} catch(e) {{}}
        """)
        page = context.new_page()
        page.set_default_timeout(TIMEOUT)
        log_info("浏览器就绪 (已预加载 Discord Token + 反检测脚本)")

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
                # 已登录态, 不强制 goto dashboard (FreezeHost 可能改了路径)
                # discover_server_ids 会自己尝试多个路径
                if "/dashboard" not in page.url and "/server" not in page.url:
                    log_info("当前不在 dashboard, discover_server_ids 会自动尝试多个路径")
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

                # 等待跳转到 Discord (精确判断域名, 不能只看 URL 含 'discord.com')
                # 因为 free.freezehost.pro/xxx?ref=discord.com 这种 URL 也会含 'discord.com'
                try:
                    page.wait_for_url(lambda u: u.startswith("https://discord.com/") or u.startswith("https://discordapp.com/"), timeout=15000)
                    log_info(f"已到达 Discord, URL: {page.url}")
                except PlaywrightTimeout:
                    # 没跳到 Discord, 截图保存
                    buf = take_screenshot(page, "not-discord")
                    current_url = page.url
                    log_error(f"未跳转到 Discord, 当前 URL: {current_url}")
                    send_tg(
                        f"用户：{display_name}\n"
                        f"❌ 点击登录后未跳转到 Discord\n"
                        f"当前 URL: {current_url}\n"
                        f"可能原因: FreezeHost 改版 / 网络问题 / 服务条款未接受\n"
                        f"\nFreezeHost Auto Renew",
                        buf,
                    )
                    raise RuntimeError(f"未跳转到 Discord, 当前 URL: {current_url}")

                # ── 关键: Discord OAuth 页面处理 ──
                # 即使 URL 含 prompt=none, Discord 仍可能显示 Authorize 按钮需要点击
                # 流程: Discord OAuth 页 → 点 Authorize → 跳 free.freezehost.pro/submitlogin → 跳 /dashboard
                # storage_state 已经预加载 Token, Discord 页面会自动登录 (Signed in as xxx)

                # 等 Discord 页面完全加载
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=15000)
                except PlaywrightTimeout:
                    log_warn("等待 Discord domcontentloaded 超时")

                # 等 networkidle 让 SPA 完全渲染 (Discord 页面较重, 需要 networkidle)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PlaywrightTimeout:
                    log_warn("等待 Discord networkidle 超时")

                page.wait_for_timeout(5000)  # 等 SPA 渲染 Authorize 按钮 (加长到 5s)
                log_info(f"Discord 页面加载完成, URL: {page.url}")

                # 检查是否需要点 Authorize 按钮
                # Discord OAuth 授权页有 "Authorize" 按钮, 必须点击才能完成授权
                authorize_clicked = False
                try:
                    # 多组选择器找 Authorize 按钮
                    authorize_selectors = [
                        'button:has-text("Authorize")',
                        'button:has-text("授权")',
                        'button[type="submit"]:has-text("Authorize")',
                        'div[class*="footer"] button[class*="primary"]',
                        'button[class*="lookFilled"]',  # Discord 蓝色主按钮
                    ]
                    for sel in authorize_selectors:
                        try:
                            btn = page.locator(sel).last
                            if btn.is_visible(timeout=2000):
                                text = btn.inner_text().strip()
                                # 排除 Cancel 按钮
                                if any(k in text.lower() for k in ("cancel", "取消", "deny")):
                                    continue
                                log_info(f"找到 OAuth 按钮: '{text}', 选择器: {sel}")
                                btn.click()
                                log_info("已点击 Authorize 按钮")
                                authorize_clicked = True
                                break
                        except Exception:
                            continue
                except Exception as e:
                    log_warn(f"查找 Authorize 按钮异常: {e}")

                if not authorize_clicked:
                    log_warn("未找到 Authorize 按钮, 可能 prompt=none 自动授权中, 等待跳转...")
                    buf = take_screenshot(page, "no-authorize-btn")
                    log_info(f"当前 URL: {page.url}")

                # 等待跳转回 FreezeHost (精确判断, 不能只看 URL 含 'free.freezehost.pro' 因为 redirect_uri 也含)
                # 必须是 URL 以 https://free.freezehost.pro 开头
                try:
                    page.wait_for_url(
                        lambda u: u.startswith("https://free.freezehost.pro/"),
                        timeout=30000,
                    )
                    log_info(f"已跳回 FreezeHost: {page.url}")
                except PlaywrightTimeout:
                    buf = take_screenshot(page, "oauth-stuck")
                    send_tg(
                        f"用户：{display_name}\n"
                        f"❌ OAuth 完成后未跳回 FreezeHost\n"
                        f"当前 URL: {page.url}\n"
                        f"是否点击 Authorize: {authorize_clicked}\n"
                        f"\nFreezeHost Auto Renew",
                        buf,
                    )
                    raise RuntimeError(f"OAuth 后未跳回 FreezeHost, URL: {page.url}")

                # 已回到 FreezeHost, 等待 /submitlogin 自动跳到 /dashboard
                # 关键: 不要强制 goto /dashboard! 这会破坏 FreezeHost session 建立
                # FreezeHost /submitlogin 是后端处理 OAuth code 的页面, 需要时间完成后端调用
                # 处理完后会自动 302 跳到 /dashboard
                def is_dashboard_or_logged_in(url):
                    """判断是否已登录: URL 是 dashboard, 或者已离开 submitlogin/callback/discord"""
                    if "/dashboard" in url:
                        return True
                    # 如果 URL 已经不是 submitlogin/callback/discord, 说明 FreezeHost 已建立 session
                    if url.startswith("https://free.freezehost.pro/"):
                        if "/submitlogin" not in url and "/callback" not in url and "/login" not in url:
                            return True
                    return False

                if not is_dashboard_or_logged_in(page.url):
                    log_info(f"等待 /submitlogin 自动跳到 /dashboard, 当前: {page.url}")
                    try:
                        page.wait_for_url(
                            lambda u: is_dashboard_or_logged_in(u),
                            timeout=60000,  # 加长到 60s, FreezeHost 后端慢
                        )
                        log_info(f"已到达: {page.url}")
                    except PlaywrightTimeout:
                        # /submitlogin 没自动跳, 不要直接 goto dashboard (会被重定向回 Discord OAuth)
                        # 而是等更久让后端处理, 然后用 JavaScript 检查 cookies / session
                        log_warn(f"60s 后仍未跳转, 当前: {page.url}")
                        # 等几秒让后端处理完
                        page.wait_for_timeout(10000)
                        # 不强制 goto dashboard, 而是重新打开 FreezeHost 首页
                        # FreezeHost 会基于 session cookie 判断已登录, 自动跳 dashboard
                        try:
                            log_info(f"尝试重新打开 FreezeHost 首页让 session 自动跳转")
                            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                            page.wait_for_timeout(5000)
                            log_info(f"重新打开后 URL: {page.url}")
                        except PlaywrightTimeout:
                            log_warn(f"重新打开首页也超时, 当前: {page.url}")

                # 最终检查登录状态 (不只看 URL, 还看页面元素)
                # 修复: 之前只判断 URL 含 /dashboard, 但 FreezeHost 可能跳到 / 或其他路径
                # 改为多维度判断: URL / 页面元素 / 是否有 Login 按钮
                logged_in = False
                if is_dashboard_or_logged_in(page.url):
                    logged_in = True
                    log_info(f"✅ URL 判断已登录: {page.url}")

                # 双重确认: 检查页面是否有 "Login with Discord" 按钮 (有 = 未登录)
                if not logged_in:
                    try:
                        login_btn = page.locator('span.text-lg:has-text("Login with Discord")')
                        if login_btn.is_visible(timeout=2000):
                            log_warn(f"❌ 页面显示 Login with Discord 按钮, 未登录")
                            logged_in = False
                        else:
                            # 没有 Login 按钮, 可能已登录但 URL 不是 /dashboard
                            log_info("未发现 Login 按钮, 判定为已登录")
                            logged_in = True
                    except Exception:
                        # 找不到按钮也判定为已登录 (FreezeHost 改版可能改了文案)
                        log_info("无法确认 Login 按钮, 默认判定为已登录")
                        logged_in = True

                if not logged_in:
                    buf = take_screenshot(page, "not-dashboard")
                    send_tg(
                        f"用户：{display_name}\n"
                        f"❌ 未登录成功\n"
                        f"当前 URL: {page.url}\n"
                        f"OAuth code 已获取但 FreezeHost 未建立 session\n"
                        f"可能原因: IP 被 FreezeHost 永久拉黑 / OAuth 流程异常\n"
                        f"\nFreezeHost Auto Renew",
                        buf,
                    )
                    raise RuntimeError(f"未到达 Dashboard, URL: {page.url}")

                # 等 dashboard 完全渲染
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PlaywrightTimeout:
                    log_warn("dashboard networkidle 超时, 继续")
                page.wait_for_timeout(3000)

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
            try:
                context.close()
            except Exception:
                pass
            browser.close()


if __name__ == "__main__":
    try:
        run()
        log_info("脚本执行完毕")
    except Exception:
        log_error("脚本失败")
        traceback.print_exc()
        sys.exit(1)
