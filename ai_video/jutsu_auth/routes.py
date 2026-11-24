"""Jutsu authentication HTTP contract, independent of the generation service."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse


def create_auth_router(authentication):
    router = APIRouter(prefix='/api/jutsu')

    @router.get('/status')
    async def status():
        return await authentication.status()

    async def authorization_url():
        try:
            return await authentication.authorize()
        except (TimeoutError, RuntimeError) as error:
            raise HTTPException(503, detail=str(error) or 'Higgsfield 인증 연결 시간이 초과되었습니다.') from None

    @router.get('/connect')
    async def connect():
        url = await authorization_url()
        return RedirectResponse(url or authentication.frontend, status_code=302)

    @router.post('/connect-browser')
    async def connect_browser():
        url = await authorization_url()
        return {'opened':False, 'authorizationUrl':url, 'status':authentication.client.status}

    @router.get('/callback')
    async def callback(request: Request):
        if request.query_params.get('error'):
            raise HTTPException(400, detail='Higgsfield 인증이 취소되거나 거절되었습니다. 다시 연결하세요.')
        try:
            authentication.answer(request.query_params.get('code'), request.query_params.get('state'))
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from None
        return RedirectResponse(authentication.frontend, status_code=302)

    return router
