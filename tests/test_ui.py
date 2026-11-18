"""
Browser UI tests: a real Chrome (headless) using the KIAROS interface the way a
person would — on the laptop (localhost) and on a phone (mobile viewport, LAN
address, pairing link). Typed commands stand in for speech.
"""

import re
import shutil

import pytest

pw = pytest.importorskip("playwright.sync_api")

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(not (shutil.which("google-chrome") or shutil.which("google-chrome-stable")),
                       reason="Google Chrome not installed"),
]

PHONE = {
    "viewport": {"width": 390, "height": 844},
    "device_scale_factor": 2,
    "is_mobile": True,
    "has_touch": True,
    "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/155.0 Mobile Safari/537.36",
}


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        # No microphone in tests: Chrome's speech recognition can listen to the real mic
        # even with fake-device flags, so it is removed from every page (see NO_MIC).
        b = p.chromium.launch(channel="chrome", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
        yield b
        b.close()


NO_MIC = "delete window.SpeechRecognition; delete window.webkitSpeechRecognition;"


def new_context(browser, **ctx):
    context = browser.new_context(**ctx)
    context.add_init_script(NO_MIC)
    return context


def open_page(browser, url: str, **ctx):
    context = new_context(browser, **ctx)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector("#conn-dot.conn-connected", timeout=25000)
    return page, errors


def close_settings_if_open(page):
    """First run (no API key) opens Settings ~2s after load; close it like a user would."""
    try:
        page.wait_for_selector("#settings-container.open", timeout=15000)
    except pw.TimeoutError:
        return  # already dismissed this session
    page.locator("#settings-close").click()
    page.wait_for_selector("#settings-container.open", state="detached", timeout=3000)
    page.wait_for_timeout(350)


def type_command(page, text: str):
    if not page.locator("#type-form.open").count():
        page.locator("#btn-keyboard").click()
    page.locator("#type-input").fill(text)
    page.locator("#type-input").press("Enter")


def last_kiaros_caption(page, pattern: str, timeout=10000):
    loc = page.locator(".caption-kiaros", has_text=re.compile(pattern, re.I)).last
    loc.wait_for(timeout=timeout)
    return loc.inner_text()


# ---------------------------------------------------------------------------
# Laptop
# ---------------------------------------------------------------------------

def test_laptop_first_run(browser, live_server):
    page, errors = open_page(browser, live_server.local)
    # Greeting appears as a caption, and settings opens because there's no API key yet.
    last_kiaros_caption(page, r"^Good (morning|afternoon|evening)")
    page.wait_for_selector(".kiaros-overlay.open", timeout=6000)
    assert page.locator("#settings-welcome").is_visible()
    assert page.locator("#status-ai.status-red").count() == 1  # no AI key yet
    assert page.locator("#input-gemini-key").is_visible()
    page.locator("#settings-close").click()
    page.wait_for_timeout(400)
    assert not page.locator(".kiaros-overlay.open").count()
    assert page.locator("#dev-laptop.online.self").count() == 1
    assert not errors, errors
    page.context.close()


def test_laptop_typed_command(browser, live_server):
    page, errors = open_page(browser, live_server.local)
    close_settings_if_open(page)
    type_command(page, "how's my laptop")
    assert page.locator(".caption-you", has_text="how's my laptop").count() == 1
    assert "percent" in last_kiaros_caption(page, r"memory")
    assert not errors, errors
    page.context.close()


def test_laptop_settings_save(browser, live_server):
    import httpx
    page, errors = open_page(browser, live_server.local)
    close_settings_if_open(page)
    try:
        _settings_save_flow(page)
    finally:
        httpx.post(f"{live_server.local}/api/settings/preferences",
                   json={"user_name": "", "honorific": "sir", "weather_location": ""})
    assert not errors, errors
    page.context.close()


def _settings_save_flow(page):
    page.locator("#btn-menu").click()
    page.locator("#btn-settings").click()
    page.wait_for_selector(".kiaros-overlay.open")
    page.locator("#input-user-name").fill("Debjyoti")
    page.locator("#input-honorific").select_option("boss")
    with page.expect_response(lambda r: r.url.endswith("/api/settings/preferences") and r.request.method == "POST"):
        page.locator("#btn-save-prefs").click()
    page.reload()
    page.wait_for_selector("#conn-dot.conn-connected")
    page.wait_for_timeout(2500)
    # Dismissed once this session, so first-run settings must NOT pop up again.
    assert not page.locator(".kiaros-overlay.open").count()
    page.locator("#btn-menu").click()
    page.locator("#btn-settings").click()
    page.wait_for_timeout(1000)
    assert page.locator("#input-user-name").input_value() == "Debjyoti"
    assert page.locator("#input-honorific").input_value() == "boss"


@pytest.mark.phone
def test_laptop_pairing_screen(browser, needs_lan):
    page, errors = open_page(browser, needs_lan.local)
    close_settings_if_open(page)
    page.locator("#btn-menu").click()
    page.locator("#btn-pair").click()
    page.wait_for_selector("#pairing-container.open")
    body = page.locator("#pair-body").inner_text()
    assert needs_lan.token in body
    assert needs_lan.lan in body
    assert not errors, errors
    page.context.close()


# ---------------------------------------------------------------------------
# Phone
# ---------------------------------------------------------------------------

@pytest.mark.phone
def test_phone_unpaired_is_refused(browser, needs_lan):
    context = new_context(browser, **PHONE)
    page = context.new_page()
    page.goto(needs_lan.lan + "/")
    page.wait_for_selector("#conn-dot.conn-unauthorized", timeout=25000)
    page.wait_for_function("document.getElementById('error-text').textContent.includes('Not paired')")
    context.close()


@pytest.mark.phone
def test_phone_pairs_and_talks(browser, needs_lan):
    page, errors = open_page(browser, f"{needs_lan.lan}/?token={needs_lan.token}", **PHONE)
    assert "token=" not in page.url  # token is stored, then removed from the address bar
    assert page.locator("body.device-phone.mode-push").count() == 1
    close_settings_if_open(page)
    assert page.locator("#status-text").inner_text() == "tap to talk"
    assert page.locator("#btn-talk").is_visible()

    # Fits the phone screen: no sideways scrolling.
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    type_command(page, "what's my battery")
    assert "battery" in last_kiaros_caption(page, "battery").lower()

    # Survives a reload without the token in the URL (it was remembered).
    page.reload()
    page.wait_for_selector("#conn-dot.conn-connected", timeout=25000)
    assert not errors, errors
    page.context.close()


@pytest.mark.phone
def test_phone_and_laptop_stay_in_sync(browser, needs_lan):
    laptop, laptop_errors = open_page(browser, needs_lan.local)
    phone, phone_errors = open_page(browser, f"{needs_lan.lan}/?token={needs_lan.token}", **PHONE)
    close_settings_if_open(laptop)
    close_settings_if_open(phone)

    # Each sees the other online.
    laptop.wait_for_selector("#dev-phone.online", timeout=5000)
    phone.wait_for_selector("#dev-laptop.online", timeout=5000)

    # Phone asks; the laptop shows the exchange, labelled "on phone".
    type_command(phone, "how's my laptop")
    mirrored = laptop.locator(".caption-you", has_text="how's my laptop").last
    mirrored.wait_for(timeout=10000)
    assert "on phone" in mirrored.inner_text().lower()
    laptop.locator(".caption-kiaros", has_text=re.compile("memory", re.I)).last.wait_for(timeout=10000)

    # Laptop asks; the phone shows it, labelled "on laptop".
    type_command(laptop, "what's my battery")
    m2 = phone.locator(".caption-you", has_text="what's my battery").last
    m2.wait_for(timeout=10000)
    assert "on laptop" in m2.inner_text().lower()

    # Laptop: "call <number>" -> phone gets a one-tap Call button (browsers need a tap).
    type_command(laptop, "call 9876501234")
    phone.wait_for_selector("#action-card", state="visible", timeout=10000)
    assert phone.locator("#action-card-btn").inner_text() == "Call 9876501234"
    phone.locator("#action-card-close").click()
    assert phone.locator("#action-card").is_hidden()

    # Phone disconnects -> laptop's phone icon goes dark.
    phone.context.close()
    laptop.wait_for_selector("#dev-phone:not(.online)", timeout=5000)
    assert not laptop_errors and not phone_errors, (laptop_errors, phone_errors)
    laptop.context.close()
