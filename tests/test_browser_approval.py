import asyncio

import pytest

from ai_video.jutsu_auth.browser_approval import APP_NAME, approve_page


class Locator:
    def __init__(self, page, matches):
        self.page, self.matches = page, matches

    def or_(self, other):
        return Locator(self.page, self.matches + other.matches)

    async def count(self):
        return len(self.matches)

    async def is_visible(self):
        return bool(self.matches)

    async def click(self, **kwargs):
        assert len(self.matches) == 1
        name = self.matches[0]
        self.page.clicks.append(name)
        if name == "계속":
            self.page.url = self.page.offer_url
            self.page.controls = [(self.page.skip_role, self.page.skip_text),
                                  ("button", "US$0 무료 체험 받기")]
        else:
            self.page.callback.set()


class Page:
    def __init__(self, callback, skip_text, skip_role, offer_url):
        self.callback = callback
        self.skip_text, self.skip_role = skip_text, skip_role
        self.offer_url = offer_url
        self.url = "https://higgsfield.ai/oauth/consent?client_id=expected"
        self.controls = [("button", "계속")]
        self.clicks = []

    def get_by_role(self, role, *, name):
        controls = [("heading", APP_NAME)] if role == "heading" else self.controls
        return Locator(self, [label for kind, label in controls
                              if kind == role and name.search(label)])


@pytest.mark.parametrize("text,role", [
    ("건너뛰고 MCP로 이동", "button"),
    ("Skip and go to MCP", "link"),
])
@pytest.mark.parametrize("offer_url", [
    "https://higgsfield.ai/mcp/onboarding",
    "https://higgsfield.ai/oauth/consent?client_id=expected",
])
def test_consent_then_skip_offer_reaches_callback(text, role, offer_url):
    async def run():
        callback = asyncio.Event()
        page = Page(callback, text, role, offer_url)
        assert await asyncio.wait_for(approve_page(page, "expected", callback), 1.5)
        assert page.clicks == ["계속", text]
        assert callback.is_set()
    asyncio.run(run())


def test_wrong_oauth_client_is_not_approved():
    async def run():
        callback = asyncio.Event()
        page = Page(callback, "건너뛰고 MCP로 이동", "button", "https://higgsfield.ai/mcp")
        assert await approve_page(page, "another-client", callback) is False
        assert page.clicks == []
    asyncio.run(run())


@pytest.mark.parametrize("url,text", [
    ("https://example.com/mcp", "건너뛰고 MCP로 이동"),
    ("https://higgsfield.ai/mcp", "US$0 무료 체험 받기"),
])
def test_does_not_click_untrusted_page_or_trial(url, text):
    async def run():
        callback = asyncio.Event()
        page = Page(callback, text, "button", url)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(approve_page(page, "expected", callback), 0.7)
        assert page.clicks == ["계속"]
    asyncio.run(run())
