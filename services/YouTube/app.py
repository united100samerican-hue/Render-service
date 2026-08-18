from __future__ import annotations
import os
from typing import Any
from fastapi import FastAPI,Header,HTTPException,Request
from fastapi.responses import JSONResponse,PlainTextResponse
from .config import Settings
from .errors import SocialMediaError
from .models import MediaRequest
from .service import SocialMediaService

settings=Settings.from_env();service=SocialMediaService(settings);app=FastAPI(title='Render Social Media Service',version='2.0')

def guard(secret:str|None)->None:
    if settings.keepalive_secret and (secret or '').strip()!=settings.keepalive_secret:raise HTTPException(status_code=403,detail='forbidden')
async def body(req:Request)->dict[str,Any]:
    try:v=await req.json();return v if isinstance(v,dict) else {}
    except Exception:return {}
def chat_id(v:dict[str,Any])->int:
    try:return int(v.get('chatId',v.get('chat_id',0)) or 0)
    except Exception:return 0
def media_request(v:dict[str,Any])->MediaRequest:
    return MediaRequest(chat_id=chat_id(v),url=str(v.get('sourceUrl',v.get('source_url',v.get('url',''))) or '').strip(),title=str(v.get('title','') or '').strip(),duration=max(0,int(v.get('duration',0) or 0)),offset=max(0,int(v.get('offset',0) or 0)),by=str(v.get('by','') or '').strip())

@app.on_event('startup')
async def startup()->None:return None
@app.on_event('shutdown')
async def shutdown()->None:await service.close()
@app.get('/')
async def root()->dict[str,Any]:return{'ok':True,'service':'social-media','ready':service.ready,'version':'2.0'}
@app.get('/ping',response_class=PlainTextResponse)
async def ping()->str:return 'OK'
@app.get('/health')
async def health()->dict[str,Any]:return{'ok':True,'ready':service.ready,'backend_error':service.backend_error,'active_sessions':service.sessions.count_active(),'cookies_configured':bool(settings.cookies_file and settings.cookies_file.is_file())}
@app.get('/state/{chat_id}')
async def state(chat_id_value:int,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret'))->dict[str,Any]:
    guard(x_keepalive_secret);return service.state(chat_id_value)
@app.post('/release')
async def release(x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret'))->dict[str,Any]:
    guard(x_keepalive_secret);return await service.release()

async def run(req:Request,secret:str|None,action:str)->Any:
    guard(secret);v=await body(req)
    try:
        if action=='pause':return await service.pause(chat_id(v))
        if action=='resume':return await service.resume(chat_id(v))
        if action=='stop':return await service.stop(chat_id(v))
        if action=='seek':
            d=int(v.get('delta',0) or 0);p=v.get('position');p=None if p is None else max(0,int(p or 0));return await service.seek(chat_id(v),d,p)
        request=media_request(v)
        if not request.chat_id or not request.url:return{'ok':False,'action':action,'error':'missing_chat_id_or_url'}
        if action=='meta':return await service.meta(request)
        if action=='start':return await service.start(request)
        if action=='next':return await service.next(request)
        if action=='skip':return await service.skip(request)
        return{'ok':False,'action':action,'error':'unsupported_action'}
    except SocialMediaError as exc:return JSONResponse(status_code=400,content={'ok':False,'action':action,'error':exc.code,'detail':exc.detail})
    except Exception as exc:return JSONResponse(status_code=502,content={'ok':False,'action':action,'error':type(exc).__name__,'detail':str(exc)})

for _action in ('meta','start','next','skip','pause','resume','seek','stop'):
    async def _handler(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret'),__action=_action):return await run(req,x_keepalive_secret,__action)
    _handler.__name__=_action
    app.post('/'+_action)(_handler)

if __name__=='__main__':
    import uvicorn
    uvicorn.run('services.YouTube.app:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')),reload=False)
