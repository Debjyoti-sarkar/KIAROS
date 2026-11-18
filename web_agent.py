"""
KIAROS web agent — a Chrome window KIAROS drives itself (Playwright).

It uses its own persistent profile (data/browser-profile), so anything you log
into there once — WhatsApp Web, Gmail, YouTube — stays logged in for KIAROS.
The window is visible so you can watch what it does.

Generic tools: open(url), read() (page text + numbered clickable elements),
click(target), type(target, text, enter).
WhatsApp: whatsapp_send(contact, message) — finds the chat by name in
WhatsApp Web, types the message, sends it, and verifies it appears.
"""

import asyncio
import logging
import os
import re

from paths import DATA_DIR

log = logging.getLogger("kiaros.web")

PROFILE_DIR = DATA_DIR / "browser-profile"
WHATSAPP_URL = os.getenv("KIAROS_WHATSAPP_URL", "https://web.whatsapp.com/")

# Collects visible interactive elements and tags them with data-kiaros-id so the
# model can say "click 7" instead of guessing selectors.
_SNAPSHOT_JS = r"""
() => {
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], ' +
              '[role=tab], [role=menuitem], [role=option], [role=textbox], [contenteditable=true], [onclick]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > innerHeight * 3) continue;
    n += 1;
    el.setAttribute('data-kiaros-id', String(n));
    const label = (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('placeholder') ||
                   el.innerText || el.value || el.getAttribute('name') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    const kind = el.tagName.toLowerCase() + (el.type ? ':' + el.type : '') +
                 (el.isContentEditable ? ':editable' : '') + (el.getAttribute('role') ? ':' + el.getAttribute('role') : '');
    out.push(`[${n}] ${kind} "${label}"`);
    if (n >= 120) break;
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 5000);
  return { title: document.title, url: location.href, text, elements: out };
}
"""


class WebAgent:
    def __init__(self):
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._ctx is not None

    async def _ensure(self):
        if self._ctx is not None:
            try:
                if self._page is None or self._page.is_closed():
                    self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
                return
            except Exception:
                self._ctx = None  # browser was closed by the user
        from playwright.async_api import async_playwright
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        if self._pw is None:
            self._pw = await async_playwright().start()
        headless = os.getenv("KIAROS_BROWSER_HEADLESS", "") == "1"
        kwargs = dict(user_data_dir=str(PROFILE_DIR), headless=headless, viewport=None,
                      args=["--start-maximized", "--no-first-run", "--no-default-browser-check"])
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception:
            self._ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        log.info("KIAROS browser started")

    async def page(self):
        await self._ensure()
        return self._page

    # -- generic tools ------------------------------------------------------

    async def open(self, url: str) -> str:
        async with self._lock:
            if not re.match(r"^[a-z]+://", url):
                url = "https://" + url
            page = await self.page()
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(1500)
            await page.bring_to_front()
            return f"Opened {page.url} — title: {await page.title()}"

    async def read(self) -> str:
        async with self._lock:
            page = await self.page()
            snap = await page.evaluate(_SNAPSHOT_JS)
            return (f"URL: {snap['url']}\nTitle: {snap['title']}\n\nVISIBLE TEXT:\n{snap['text']}\n\n"
                    f"INTERACTIVE ELEMENTS (use the number to click/type):\n" + "\n".join(snap["elements"]))

    async def _locate(self, target: str):
        page = await self.page()
        target = str(target).strip()
        if target.isdigit():
            loc = page.locator(f'[data-kiaros-id="{target}"]')
            if await loc.count():
                return loc.first
            raise ValueError(f"No element numbered {target}; call browser_read again.")
        for loc in (page.get_by_role("button", name=re.compile(re.escape(target), re.I)),
                    page.get_by_role("link", name=re.compile(re.escape(target), re.I)),
                    page.get_by_label(re.compile(re.escape(target), re.I)),
                    page.get_by_placeholder(re.compile(re.escape(target), re.I)),
                    page.get_by_text(target, exact=False)):
            try:
                if await loc.count():
                    return loc.first
            except Exception:
                continue
        raise ValueError(f"Couldn't find '{target}' on the page.")

    async def describe(self, target: str) -> dict:
        """What an element is: {'label', 'kind', 'password'} — used for safety checks before acting."""
        try:
            el = await self._locate(target)
            return await el.evaluate("""e => ({
                label: (e.getAttribute('aria-label') || e.getAttribute('title') || e.innerText || e.value ||
                        e.getAttribute('placeholder') || '').trim().slice(0, 120),
                kind: e.tagName.toLowerCase() + (e.type ? ':' + e.type : ''),
                password: e.type === 'password' || /password|passcode|\bpin\b|otp/i.test(
                    (e.getAttribute('autocomplete') || '') + ' ' + (e.getAttribute('name') || '') + ' ' +
                    (e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('placeholder') || '')),
            })""")
        except Exception:
            return {"label": str(target), "kind": "", "password": False}

    async def click(self, target: str) -> str:
        async with self._lock:
            el = await self._locate(target)
            await el.click(timeout=10000)
            page = await self.page()
            await page.wait_for_timeout(1500)
            return f"Clicked '{target}'. Now at {page.url}"

    async def type(self, target: str, text: str, enter: bool = False) -> str:
        async with self._lock:
            el = await self._locate(target)
            await el.click(timeout=10000)
            page = await self.page()
            try:
                await el.fill(text, timeout=5000)
            except Exception:  # contenteditable or custom inputs
                await page.keyboard.press("Control+A")
                await page.keyboard.type(text, delay=15)
            if enter:
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2000)
            return f"Typed into '{target}'" + (" and pressed Enter." if enter else ".")

    # -- WhatsApp Web -------------------------------------------------------

    async def whatsapp_status(self, wait: float = 40) -> str:
        """'ready', 'login' (QR code showing) or 'loading'."""
        page = await self.page()
        if not page.url.startswith(WHATSAPP_URL):
            await page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=45000)
        ready = page.locator('#pane-side, [aria-label="Chat list"], [data-testid="chat-list"]')
        qr = page.locator('canvas[aria-label*="Scan" i], [data-ref] canvas, [data-testid="qrcode"]')
        login_text = page.get_by_text(re.compile(r"(Log into|Use) WhatsApp on your computer|Scan to log in|Steps to log in", re.I))
        for _ in range(int(wait * 2)):
            if await ready.count():
                return "ready"
            if await qr.count() or await login_text.count():
                return "login"
            await page.wait_for_timeout(500)
        return "loading"

    async def whatsapp_send(self, contact: str, message: str, on_wait=None, login_wait: float = 180) -> str:
        async with self._lock:
            page = await self.page()
            status = await self.whatsapp_status()
            await page.bring_to_front()
            if status == "login":
                # Wait for the user to link WhatsApp instead of giving up, then carry on by ourselves.
                if on_wait:
                    await on_wait("WhatsApp Web needs linking once. On your phone open WhatsApp, Settings, Linked "
                                  "devices, Link a device, and scan the code in the KIAROS browser. I'll carry on "
                                  "as soon as you do.")
                ready = page.locator('#pane-side, [aria-label="Chat list"], [data-testid="chat-list"]')
                for _ in range(int(login_wait * 2)):
                    if await ready.count():
                        status = "ready"
                        await page.wait_for_timeout(3000)  # let chats sync after linking
                        break
                    await page.wait_for_timeout(500)
                else:
                    return ("NEEDS_LOGIN: WhatsApp Web isn't linked yet. On your phone open WhatsApp → Settings → "
                            "Linked devices → Link a device, and scan the QR code in the KIAROS browser window. "
                            "Then say continue.")
            if status != "ready":
                return "WhatsApp Web didn't finish loading. Check the internet connection and try again."

            search = page.locator('[aria-label="Search input textbox"], [aria-label*="Search" i][contenteditable="true"], '
                                  '#side [contenteditable="true"], [data-tab="3"][contenteditable="true"], '
                                  '#side input[type="text"], [role="textbox"][aria-label*="search" i]').first
            await search.click(timeout=10000)
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.type(contact, delay=30)
            await page.wait_for_timeout(2000)

            # Pick the best matching chat: exact name, then starts-with, then contains.
            titles = page.locator('#pane-side span[title], [aria-label="Search results."] span[title], '
                                  '[aria-label*="Chat list" i] span[title]')
            names = [(await titles.nth(i).get_attribute("title")) or "" for i in range(min(await titles.count(), 25))]
            low = contact.lower().strip()
            choice = next((n for n in names if n.lower() == low), None) or \
                next((n for n in names if n.lower().startswith(low)), None) or \
                next((n for n in names if low in n.lower()), None)
            if not choice:
                await page.keyboard.press("Escape")
                return f"I couldn't find a WhatsApp chat named {contact}."
            await titles.nth(names.index(choice)).click()
            await page.wait_for_timeout(1200)

            box = page.locator('footer [contenteditable="true"], [aria-label="Type a message"], '
                               '[aria-placeholder="Type a message"], [data-tab="10"][contenteditable="true"]').last
            await box.click(timeout=10000)
            await page.keyboard.type(message, delay=20)
            await page.keyboard.press("Enter")

            # Verify: the message shows up as an outgoing bubble.
            sent = page.locator('.message-out, [data-testid="msg-container"] .message-out, div[class*="message-out"]')
            probe = message.strip()[:40]
            for _ in range(20):
                await page.wait_for_timeout(500)
                n = await sent.count()
                if n and probe.lower() in ((await sent.nth(n - 1).inner_text()) or "").lower():
                    return f"Sent to {choice} on WhatsApp: \"{message}\""
            return f"I typed the message to {choice} and pressed send, but couldn't confirm it went through. Check the KIAROS browser."

    async def close(self):
        try:
            if self._ctx:
                await self._ctx.close()
        finally:
            self._ctx = self._page = None
            if self._pw:
                await self._pw.stop()
                self._pw = None


web = WebAgent()
