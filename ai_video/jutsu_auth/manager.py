"""Application-independent owner of Jutsu authentication and restoration."""
import asyncio
import logging

from .browser_approval import approve_with_browser

DEFAULT_FRONTEND = 'https://api-aipro.chatbaram.com/v1/previs/previs-console01.html'
DEFAULT_REDIRECT = 'https://api-aipro.chatbaram.com/v1/previs/api/jutsu/callback'
logger = logging.getLogger('uvicorn.error')


class AuthenticationManager:
    def __init__(self, client, *, config=None, frontend=None):
        self.client = client
        config = config or {}
        self.frontend = frontend or config.get('JUTSU_FRONTEND_URL', DEFAULT_FRONTEND)
        self.redirect_url = config.get('JUTSU_OAUTH_REDIRECT_URL', DEFAULT_REDIRECT)
        self.browser_profile = config.get('JUTSU_AUTH_BROWSER_PROFILE', '').strip()
        self.headless = config.get('JUTSU_AUTH_BROWSER_HEADLESS') == '1'
        self.browser_auto_approve = config.get('JUTSU_AUTH_BROWSER_AUTO_APPROVE', '0') == '1'
        self.ready_timeout = float(config.get('JUTSU_AUTH_READY_TIMEOUT_SECONDS', 300))
        if not 0 < self.ready_timeout <= 600:
            raise ValueError('JUTSU_AUTH_READY_TIMEOUT_SECONDS must be between 0 and 600')
        self._check_lock = asyncio.Lock()
        self._browser_lock = asyncio.Lock()
        self._last_browser_flow = None
        self._task = None
        self._wake = asyncio.Event()

    @classmethod
    def from_config(cls, config, *, client=None):
        if client is None:
            from . import create_client
            client = create_client(config.get('JUTSU_OAUTH_REDIRECT_URL', DEFAULT_REDIRECT),
                                   data_directory=config.get('JUTSU_AUTH_DATA_DIR') or None)
        return cls(client, config=config)

    async def ensure(self):
        """Serialize restoration/ping without retrying any scene mutation."""
        async with self._check_lock:
            previous_state = self.client.status['state']
            if self.client.status['state'] == 'disconnected':
                self.client.status.update(state='connecting', message='저장된 인증으로 Higgsfield 연결을 확인하고 있습니다.')
            try:
                checker = getattr(self.client, 'check_connection', None)
                if checker is not None:
                    await checker()
                else:
                    await self.client.start()
            except Exception:
                # Preserve an active OAuth flow instead of discarding its callback.
                if self.client.status['state'] != 'authorizing':
                    self.client.status.update(state='disconnected', message='Higgsfield 연결을 자동으로 다시 확인합니다.')
                if self.client.status['state'] != previous_state:
                    self._wake.set()
                raise
            if self.client.status['state'] != previous_state:
                self._wake.set()
        return self.client.status

    async def status(self):
        if self.client.status['state'] == 'disconnected':
            try:
                await self.ensure()
            except (TimeoutError, RuntimeError):
                pass
        manual = self.client.status['state'] == 'authorizing' and not self.browser_auto_approve
        return {**self.client.status, 'managedBy':'backend',
                'browserAutoApprovalEnabled':self.browser_auto_approve,
                **({'message':'Higgsfield 연결 버튼을 눌러 일반 브라우저에서 로그인·승인을 완료하세요.'} if manual else {}),
                'authenticationRequired':self.client.status['state'] == 'authorizing'}

    async def authorize(self):
        return await self.client.authorize()

    def answer(self, code, state):
        self.client.storage.answer(code, state)

    async def approve_pending(self):
        if not self.browser_auto_approve or self.client.status['state'] != 'authorizing' or not self.browser_profile:
            return
        async with self._browser_lock:
            url = await self.client.authorize()
            if not url or url == self._last_browser_flow:
                return
            # Each OAuth state gets one browser attempt; a new state is not delayed
            # by the previous flow's cooldown. Never log the authorization URL.
            self._last_browser_flow = url
            try:
                await approve_with_browser(url, getattr(self.client, 'redirect_url', self.redirect_url),
                                           self.browser_profile, headless=self.headless)
            except Exception as error:
                logger.warning('[previs-auth] 브라우저 자동 승인 미완료 (%s)', type(error).__name__)

    async def maintain(self):
        previous = None
        while True:
            state = 'disconnected'
            try:
                await self.ensure()
                state = self.client.status['state']
                marker = (state, self.client.status.get('message'))
                if marker != previous:
                    logger.info('[previs-auth] state=%s %s', state, self.client.status.get('message', ''))
                    previous = marker
                await self.approve_pending()
                state = self.client.status['state']
            except Exception as error:
                marker = ('check_error', type(error).__name__)
                if marker != previous:
                    logger.warning('[previs-auth] 인증 상태 점검 재시도 예정 (%s)', type(error).__name__)
                    previous = marker
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1800 if state == 'connected' else 5)
            except TimeoutError:
                pass
            self._wake.clear()

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.maintain())

    async def require_ready(self, *, timeout=None):
        """Wait for tools-ready, not merely for an OAuth redirect/start event."""
        previous = None
        try:
            async with asyncio.timeout(self.ready_timeout if timeout is None else timeout):
                await self.ensure()
                while self.client.status['state'] != 'connected':
                    state = self.client.status['state']
                    if state == 'unavailable':
                        raise RuntimeError(self.client.status['message'])
                    if state != previous:
                        logger.info('[previs-auth] 생성 준비 대기 · state=%s', state)
                        previous = state
                    await asyncio.sleep(.2)
                    if self.client.status['state'] == 'disconnected':
                        await self.ensure()
        except TimeoutError:
            raise RuntimeError('Higgsfield 인증·연결 준비 대기 시간이 초과되었습니다. 인증 상태를 확인하세요.') from None

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.client.close()


async def require_connection(client, *, authentication=None):
    """Shared entry for pipelines and standalone camera-reference consumers."""
    manager = authentication if authentication is not None else AuthenticationManager(client)
    await manager.require_ready()
