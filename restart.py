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
SCREENSHOT_DIR = Path("screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

BASE_URL   = "https://free.freezehost.pro"
VIEWPORT_W = 1280
VIEWPORT_H = 753

_SENSITIVE_VALUES: set[str] = set()
_SERVER_INDEX: dict[str, int] = {}

# ── 原始值存储（用于 TG 明文推送） ──────────────────────
_RAW_EMAIL: str = ""
_RAW_SERVER_IDS: dict[str, str] = {}   # server_id -> server_id（保留原始ID）


def _register_sensitive(*values):
    for v in values:
        if v and len(v) > 2:
            _SENSITIVE_VALUES.add(v)


def _server_label(server_id: str) -> str:
    if server_id not in _SERVER_INDEX:
        _SERVER_INDEX[server_id] = len(_SERVER_INDEX) + 1
    _RAW_SERVER_IDS[server_id] = server_id
    return f"服务器#{_SERVER_INDEX[server_id]}"


def _mask(text: str) -> str:
    """日志脱敏：隐藏所有敏感信息"""
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


def extract_email(page) -> str | None:
    global _RAW_EMAIL
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
            _RAW_EMAIL = email
            _register_sensitive(email)
            log_info(f"邮箱获取成功: {email}")
            return email
        log_warn("Settings 页面未找到邮箱")
        return None
    except Exception as e:
        log_warn(f"获取邮箱失败: {e}")
        return None


def send_tg(caption: str, image_bytes: bytes | None = None):
    """发送 TG 通知，caption 应为明文（不脱敏）"""
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
        for sel in ['button:has-text("Authorize")', 'button:has-text("授权")',
                    'button[type="submit"]', 'div[class*="footer"] button', 'button[class*="primary"]']:
            try:
                btn = page.locator(sel).last
                if not btn.is_visible():
                    continue
                text = btn.inner_text().strip()
                if any(k in text.lower() for k in ("取消", "cancel", "deny")):
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
            log_info(f"第 {attempt + 1} 次重试...")
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

        log_warn(f"第 {attempt + 1} 次未发现服务器")
        take_screenshot(page, f"dashboard-empty-{attempt + 1}")
        if attempt < 2:
            page.wait_for_timeout(3000)

    return []


def detect_server_status(page) -> str:
    """
    检测服务器当前电源状态。
    返回: "running" | "stopped" | "starting" | "stopping" | "unknown"
    """
    status = page.evaluate("""() => {
        // 方法1: 检查 power-btn 文字
        const powerBtn = document.getElementById('power-btn');
        if (powerBtn) {
            const text = powerBtn.innerText.trim().toLowerCase();
            if (text.includes('stop server') || text.includes('stop')) return 'running';
            if (text.includes('start server') || text.includes('start')) return 'stopped';
        }

        // 方法2: 检查 restart-btn 可见性
        const restartBtn = document.getElementById('restart-btn');
        if (restartBtn) {
            const display = restartBtn.style.display || getComputedStyle(restartBtn).display;
            if (display === 'flex' || display === 'inline-flex' || display === 'block') return 'running';
            if (display === 'none') return 'stopped';
        }

        // 方法3: 检查页面状态文字
        const body = document.body.innerText.toLowerCase();
        if (body.includes('server is running') || body.includes('status: running')) return 'running';
        if (body.includes('server is offline') || body.includes('status: offline') || body.includes('server is stopped')) return 'stopped';
        if (body.includes('starting')) return 'starting';
        if (body.includes('stopping')) return 'stopping';

        // 方法4: 通过按钮颜色/类名判断
        if (powerBtn) {
            const cls = powerBtn.className || '';
            if (cls.includes('red')) return 'running';
            if (cls.includes('blue')) return 'stopped';
        }

        return 'unknown';
    }""")
    return status or "unknown"


def wait_for_status_change(page, target_status: str, max_wait: int = 60000) -> str:
    """等待服务器状态变化到目标状态"""
    interval = 3000
    elapsed = 0
    while elapsed < max_wait:
        page.wait_for_timeout(interval)
        elapsed += interval
        current = detect_server_status(page)
        log_info(f"  等待状态变化... 当前: {current} (已等待 {elapsed // 1000}s)")
        if current == target_status:
            return current
    return detect_server_status(page)


def send_power_command_via_page(page, command: str) -> bool:
    """
    通过页面 JS 调用 sendPowerCommand 函数。
    command: 'start' | 'restart' | 'stop' | 'kill'
    """
    result = page.evaluate(f"""() => {{
        if (typeof sendPowerCommand === 'function') {{
            sendPowerCommand('{command}');
            return 'called';
        }}
        return 'not_found';
    }}""")

    if result == "called":
        log_info(f"已调用 sendPowerCommand('{command}')")
        return True

    log_warn(f"sendPowerCommand 函数未找到，尝试点击按钮...")

    if command == "restart":
        try:
            btn = page.locator("#restart-btn")
            if btn.is_visible():
                btn.click()
                log_info("已点击 Restart 按钮")
                return True
        except Exception:
            pass
        try:
            btn = page.locator("button:has(i.fa-sync-alt)").first
            if btn.is_visible():
                btn.click()
                log_info("已点击 sync-alt 图标按钮")
                return True
        except Exception:
            pass

    elif command == "start":
        try:
            btn = page.locator("#power-btn")
            if btn.is_visible():
                text = btn.inner_text().strip().lower()
                if "start" in text:
                    btn.click()
                    log_info("已点击 Start Server 按钮")
                    return True
        except Exception:
            pass
        try:
            btn = page.locator('button:has-text("Start Server")').first
            if btn.is_visible():
                btn.click()
                log_info("已点击 Start Server 文字按钮")
                return True
        except Exception:
            pass

    elif command == "stop":
        try:
            btn = page.locator("#power-btn")
            if btn.is_visible():
                text = btn.inner_text().strip().lower()
                if "stop" in text:
                    btn.click()
                    log_info("已点击 Stop Server 按钮")
                    return True
        except Exception:
            pass

    elif command == "kill":
        try:
            btn = page.locator("#btn-kill")
            if btn.is_visible():
                btn.click()
                log_info("已点击 Kill 按钮")
                return True
        except Exception:
            pass

    log_warn(f"未能执行 '{command}' 命令")
    return False


# ── 状态中文映射 ──────────────────────────────────────────
def _state_cn(state: str) -> str:
    return {
        "running": "运行中",
        "stopped": "关机",
        "starting": "启动中",
        "stopping": "关机中",
        "unknown": "未知",
    }.get(state, state)


def process_server(page, server_id: str) -> dict:
    tag = _server_label(server_id)
    server_url = f"{BASE_URL}/server-console?id={server_id}"
    result = dict(server_id=server_id, status="unknown", before_state=None, after_state=None,
                  emoji="❓", status_label="未知", detail="")

    log_info(f"[{tag}] 开始处理")
    try:
        page.goto(server_url, wait_until="networkidle")
        page.wait_for_timeout(5000)

        current_state = detect_server_status(page)
        result["before_state"] = current_state
        log_info(f"[{tag}] 当前状态: {current_state}")

        if current_state == "running":
            log_info(f"[{tag}] 服务器运行中，执行重启...")
            success = send_power_command_via_page(page, "restart")
            if success:
                page.wait_for_timeout(5000)
                page.wait_for_timeout(10000)
                after_state = detect_server_status(page)
                result["after_state"] = after_state
                log_info(f"[{tag}] 重启后状态: {after_state}")

                if after_state in ("running", "starting"):
                    result.update(status="restarted", emoji="🔄", status_label="重启成功",
                                  detail=f"{_state_cn('running')} → 重启 → {_state_cn(after_state)}")
                else:
                    result.update(status="restart_uncertain", emoji="⚠️", status_label="重启状态不确定",
                                  detail=f"{_state_cn('running')} → 重启 → {_state_cn(after_state)}")
            else:
                result.update(status="restart_failed", emoji="❌", status_label="重启失败",
                              detail="无法触发重启命令")

        elif current_state == "stopped":
            log_info(f"[{tag}] 服务器已关机，执行开机...")
            success = send_power_command_via_page(page, "start")
            if success:
                page.wait_for_timeout(5000)
                after_state = wait_for_status_change(page, "running", max_wait=60000)
                result["after_state"] = after_state
                log_info(f"[{tag}] 开机后状态: {after_state}")

                if after_state in ("running", "starting"):
                    result.update(status="started", emoji="✅", status_label="开机成功",
                                  detail=f"{_state_cn('stopped')} → 开机 → {_state_cn(after_state)}")
                else:
                    result.update(status="start_uncertain", emoji="⚠️", status_label="开机状态不确定",
                                  detail=f"{_state_cn('stopped')} → 开机 → {_state_cn(after_state)}")
            else:
                result.update(status="start_failed", emoji="❌", status_label="开机失败",
                              detail="无法触发开机命令")

        elif current_state in ("starting", "stopping"):
            log_info(f"[{tag}] 服务器处于过渡状态 ({current_state})，等待稳定...")
            page.wait_for_timeout(15000)
            stable_state = detect_server_status(page)
            log_info(f"[{tag}] 等待后状态: {stable_state}")

            if stable_state == "running":
                log_info(f"[{tag}] 服务器已运行，执行重启...")
                success = send_power_command_via_page(page, "restart")
                if success:
                    page.wait_for_timeout(15000)
                    after_state = detect_server_status(page)
                    result["after_state"] = after_state
                    result.update(status="restarted", emoji="🔄", status_label="重启成功",
                                  detail=f"{_state_cn(current_state)} → {_state_cn('running')} → 重启 → {_state_cn(after_state)}")
                else:
                    result.update(status="restart_failed", emoji="❌", status_label="重启失败",
                                  detail=f"等待后{_state_cn('running')}，但无法触发重启")
            elif stable_state == "stopped":
                log_info(f"[{tag}] 服务器已关机，执行开机...")
                success = send_power_command_via_page(page, "start")
                if success:
                    page.wait_for_timeout(5000)
                    after_state = wait_for_status_change(page, "running", max_wait=60000)
                    result["after_state"] = after_state
                    result.update(status="started", emoji="✅", status_label="开机成功",
                                  detail=f"{_state_cn(current_state)} → {_state_cn('stopped')} → 开机 → {_state_cn(after_state)}")
                else:
                    result.update(status="start_failed", emoji="❌", status_label="开机失败",
                                  detail=f"等待后{_state_cn('stopped')}，但无法触发开机")
            else:
                result.update(status="transition", emoji="⏳", status_label="状态过渡中",
                              detail=f"{_state_cn(current_state)} → {_state_cn(stable_state)}")

        else:
            log_warn(f"[{tag}] 无法确定服务器状态: {current_state}")
            log_info(f"[{tag}] 尝试执行开机...")
            success = send_power_command_via_page(page, "start")
            if success:
                page.wait_for_timeout(10000)
                after_state = detect_server_status(page)
                result["after_state"] = after_state
                result.update(status="attempted_start", emoji="❓", status_label="尝试开机",
                              detail=f"{_state_cn('unknown')} → 尝试开机 → {_state_cn(after_state)}")
            else:
                result.update(status="unknown", emoji="❓", status_label="状态未知",
                              detail="无法确定状态且无法操作")

    except Exception as e:
        log_error(f"[{tag}] 异常: {e}")
        result.update(status="error", emoji="❌", status_label="脚本异常",
                      detail=str(e)[:80])

    return result


def build_tg_message(display_name: str, results: list[dict]) -> str:
    """
    构建 TG 推送消息 —— 使用完整明文信息。
    display_name: 原始邮箱
    results: process_server 返回的结果列表
    """
    lines = [f"用户：{display_name}"]

    for r in results:
        sid = r["server_id"]
        line = f"{sid} | {r['emoji']}{r['status_label']}"
        if r["detail"]:
            line += f" | {r['detail']}"
        lines.append(line)

    lines.append("")
    lines.append("FreezeHost Auto Restart")
    return "\n".join(lines)


#  主流程
def run():
    global _RAW_EMAIL

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

            # ── 登录 ─────────────────────────────────────
            log_info("打开 FreezeHost 登录页")
            page.goto(BASE_URL, wait_until="domcontentloaded")
            page.click('span.text-lg:has-text("Login with Discord")')

            confirm_btn = page.locator("button#confirm-login")
            confirm_btn.wait_for(state="visible")
            confirm_btn.click()
            log_info("已接受服务条款")

            page.wait_for_url(re.compile(r"discord\.com"), timeout=15000)
            log_info("已到达 Discord")

            # ── 注入 Token ────────────────────────────────
            page.evaluate("""(token) => {
                const f = document.createElement('iframe');
                f.style.display = 'none';
                document.body.appendChild(f);
                f.contentWindow.localStorage.setItem('token', '"'+token+'"');
                try { localStorage.setItem('token', '"'+token+'"'); } catch(e) {}
                document.body.removeChild(f);
            }""", DISCORD_TOKEN)
            log_info("Token 已注入")

            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(3000)

            if re.search(r"discord\.com/login", page.url):
                take_screenshot(page, "token-failed")
                raise RuntimeError("Token 登录失败")

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
                        take_screenshot(page, "oauth-stuck")
                        raise RuntimeError("OAuth 未跳转")
            except PlaywrightTimeout:
                if "discord.com" in page.url:
                    raise RuntimeError("OAuth 超时")

            # ── Dashboard ─────────────────────────────────
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

            # ── 邮箱 ─────────────────────────────────────
            email = extract_email(page)
            if email:
                display_name = email      # 日志中会被 _mask 隐藏
            else:
                log_warn("邮箱获取失败，TG 将显示「未知用户」")

            # ── 发现服务器 ────────────────────────────────
            server_ids = discover_server_ids(page)
            if not server_ids:
                buf = take_screenshot(page, "no-servers")
                tg_name = _RAW_EMAIL or "未知用户"
                send_tg(f"用户：{tg_name}\n⚠️ 未发现服务器\n\nFreezeHost Auto Restart", buf)
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

            # ── TG 推送（明文完整信息） ──────────────────
            tg_name = _RAW_EMAIL or "未知用户"
            tg_msg = build_tg_message(tg_name, results)

            # 日志中打印脱敏版
            log_info(f"TG 消息内容:\n{_mask(tg_msg)}")

            send_tg(tg_msg, final_img)
            log_info("所有服务器处理完毕")

        except Exception as e:
            buf = take_screenshot(page, "fatal-error")
            tg_name = _RAW_EMAIL or "未知用户"
            send_tg(f"用户：{tg_name}\n❌ 异常: {e}\n\nFreezeHost Auto Restart", buf)
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
