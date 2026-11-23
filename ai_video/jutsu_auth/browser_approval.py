"""Optional browser approval for the configured Jutsu OAuth client."""

import asyncio
import logging
import re
from urllib.parse import parse_qs, urlparse


APP_NAME = "3D Jutsu Previs Console Python"
CONSENT = re.compile(r"^https://(?:www\.)?higgsfield\.ai/(?:[a-z]{2}(?:-[A-Z]{2})?/)?oauth/consent(?:[?#]|$)")
CONTINUE = re.compile(r"^(계속|Continue)$", re.IGNORECASE)
SKIP_TO_MCP = re.compile(
    r"^(?:건너뛰고\s*MCP로\s*이동|Skip\s+(?:and\s+)?(?:go\s+)?to\s+MCP)$",
    re.IGNORECASE,
)
GOOGLE_SIGN_IN = re.compile(
    r"^(?:(?:Continue|Sign in|Log in) with Google|"
    r"(?:Google|구글)\s*(?:계정)?\s*(?:으?로)?\s*(?:계속|로그인)(?:하기)?|Google)$",
    re.IGNORECASE,
)
HIGGSFIELD_HOSTS = {"clerk.higgsfield.ai", "higgsfield.ai", "www.higgsfield.ai"}
logger = logging.getLogger("uvicorn.error")


def authorization_client(url, redirect):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    clients = query.get("client_id", [])
    if (
        parsed.scheme == "https"
        and parsed.netloc in HIGGSFIELD_HOSTS
        and query.get("redirect_uri") == [redirect]
        and len(clients) == 1
        and len(query.get("state", [])) == 1
    ):
        return clients[0]
    return None


async def approve_page(page, client_id, callback):
    google_clicked = False
    consent_clicked = False
    skip_clicked = False
    while not callback.is_set():
        current = page.url
        if CONSENT.match(current):
            if parse_qs(urlparse(current).query).get("client_id") != [client_id]:
                logger.warning("[previs-auth] 자동 승인 중단 · 승인 화면의 앱이 일치하지 않습니다.")
                return False
            heading = page.get_by_role("heading", name=re.compile(re.escape(APP_NAME)))
            if not consent_clicked and await heading.is_visible() and page.url == current:
                await page.get_by_role("button", name=CONTINUE).click(timeout=10000)
                consent_clicked = True
                logger.info("[previs-auth] Higgsfield 앱 연결 동의 완료 · 콜백 대기")
                continue
        # The offer can replace the consent UI without changing its URL.
        parsed = urlparse(current)
        if parsed.scheme == "https" and parsed.netloc in HIGGSFIELD_HOSTS:
            skip = page.get_by_role("button", name=SKIP_TO_MCP).or_(
                page.get_by_role("link", name=SKIP_TO_MCP)
            )
            if not skip_clicked and await skip.count() == 1 and await skip.is_visible() and page.url == current:
                await skip.click(timeout=10000)
                skip_clicked = True
                logger.info("[previs-auth] 요금제 안내 건너뛰기 · MCP 인증 계속")
                continue
            google = page.get_by_role("button", name=GOOGLE_SIGN_IN).or_(
                page.get_by_role("link", name=GOOGLE_SIGN_IN)
            )
            if not google_clicked and await google.count() == 1 and await google.is_visible() and page.url == current:
                google_clicked = True
                await google.click(timeout=10000)
                logger.info("[previs-auth] Higgsfield Google 로그인 선택")
        await asyncio.sleep(0.3)
    return True


async def approve_with_browser(url, redirect, profile, *, headless=False):
    client_id = authorization_client(url, redirect)
    if not client_id:
        logger.warning("[previs-auth] 자동 승인 중단 · 인증 요청 주소가 설정과 일치하지 않습니다.")
        return

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(profile, headless=headless)
        try:
            page = await context.new_page()
            callback = asyncio.Event()

            def received(response):
                parsed = urlparse(response.url)
                expected = urlparse(redirect)
                if (parsed.scheme, parsed.netloc, parsed.path) == (expected.scheme, expected.netloc, expected.path):
                    query = parse_qs(parsed.query)
                    if response.status == 302 and query.get("state") == parse_qs(urlparse(url).query)["state"]:
                        callback.set()

            page.on("response", received)
            async with asyncio.timeout(260):
                await page.goto(url, wait_until="domcontentloaded")
                if await approve_page(page, client_id, callback):
                    logger.info("[previs-auth] 브라우저 권한 승인 및 콜백 도착")
        finally:
            await context.close()
