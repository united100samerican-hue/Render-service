from __future__ import annotations
import os
from typing import Any
from fastapi import FastAPI,Header,HTTPException,Request
from fastapi.responses import JSONResponse,PlainTextResponse
from .config import Settings
from .errors import SocialMediaError
from .models import MediaRequest
from .service import SocialMediaService
settings=Settings.from_env();service=SocialMediaService(settings);app=FastAPI(title='Render Social Media Service',version='1.0')
def guard(secret):
    if settings.keepalive_secret and (secret or '').strip()!=settings.keepalive_secret:raise HTTPException(status_code=403,detail='forbidden')
async def body(req):
    try:
        x=await req.json();return x if isinstance(x,dict) else {}
    except Exception:return {}
def req_from(x):return MediaRequest(int(x.get('chatId',x.get('chat_id',0)) or 0),str(x.get('sourceUrl',x.get('source_url',x.get('url',''))) or '').strip(),str(x.get('title','') or '').strip(),max(0,int(x.get('duration',0) or 0)),max(0,int(x.get('offset',0) or 0)),str(x.get('by','') or '').strip())
@app.on_event('startup')
async def startup():await service.ensure_ready()
@app.on_event('shutdown')
async def shutdown():await service.close()
@app.get('/')
async def root():return {'ok':True,'service':'social-media','ready':service.ready}
@app.get('/ping',response_class=PlainTextResponse)
async def ping():return 'OK'
@app.get('/health')
async def health():return {'ok':True,'ready':service.ready,'backend_error':service.backend_error}
@app.get('/state/{chat_id}')
async def state(chat_id:int,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):guard(x_keepalive_secret);return service.state(chat_id)
async def run(req:Request,secret,action):
    guard(secret);b=await body(req)
    try:
        if action=='pause':return await service.pause(int(b.get('chatId',b.get('chat_id',0)) or 0))
        if action=='resume':return await service.resume(int(b.get('chatId',b.get('chat_id',0)) or 0))
        if action=='stop':return await service.stop(int(b.get('chatId',b.get('chat_id',0)) or 0))
        if action=='seek':
            cid=int(b.get('chatId',b.get('chat_id',0)) or 0);delta=int(b.get('delta',0) or 0);pos=b.get('position');return await service.seek(cid,delta,None if pos is None else max(0,int(pos or 0)))
        r=req_from(b)
        if not r.chat_id or not r.url:return {'ok':False,'action':action,'error':'missing_chat_id_or_url'}
        return await {'meta':service.meta,'start':service.start,'next':service.next,'skip':service.skip}[action](r)
    except SocialMediaError as e:return JSONResponse(status_code=400,content={'ok':False,'action':action,'error':e.code,'detail':e.detail})
    except Exception as e:return JSONResponse(status_code=502,content={'ok':False,'action':action,'error':type(e).__name__,'detail':str(e)})
for name in ('meta','start','next','skip','pause','resume','seek','stop'):
    async def handler(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret'),_name=name):return await run(req,x_keepalive_secret,_name)
    app.add_api_route('/'+name,handler,methods=['POST'])
if __name__=='__main__':
    import uvicorn;uvicorn.run('services.YouTube.app:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')),reload=False)
