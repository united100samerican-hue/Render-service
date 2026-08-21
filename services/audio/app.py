from __future__ import annotations
import logging
import os
from typing import Any
from fastapi import FastAPI,Header,HTTPException,Request
from fastapi.responses import PlainTextResponse
from service import service
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log=logging.getLogger('audio_app')
app=FastAPI(title='Render Audio+URL Service',version='6.2')
SECRET=os.getenv('KEEPALIVE_SECRET','').strip()
def guard(value:str|None):
    if SECRET and (value or '').strip()!=SECRET:raise HTTPException(403,'forbidden')
async def body(req:Request)->dict[str,Any]:
    try:
        x=await req.json();return x if isinstance(x,dict) else {}
    except Exception:return {}
def pick(b,*names,default=None):
    for n in names:
        if n in b and b[n] is not None:return b[n]
    return default
def cid(b):
    try:return int(pick(b,'chatId','chat_id',default=0))
    except:return 0
def stype(b):
    raw=str(pick(b,'sourceType','source_type',default='')).strip().lower().replace('-','_')
    if raw in {'link','url','youtube','yt','youtube_video'}:return 'url'
    if raw in {'audio','voice','music','song','telegram_audio'}:return 'telegram_audio'
    if raw in {'video','clip','movie','telegram_video'}:return 'telegram_video'
    if raw in {'telegram','tg','telegram_file_id','telegram_document','document','file','media','file_id'}:return 'telegram_file_id'
    if raw=='telegram_message':return raw
    raise HTTPException(400,f'unsupported_source_type: {raw or "missing"}')
def sid(b):return str(pick(b,'sourceId','source_id','sourceUrl','source_url',default='')).strip()
def scid(b):
    try:return int(pick(b,'sourceChatId','source_chat_id',default=0))
    except:return 0
def smid(b):
    try:return int(pick(b,'sourceMessageId','source_message_id',default=0))
    except:return 0
def intval(b,*names):
    try:return int(pick(b,*names,default=0) or 0)
    except:return 0
def title(b):return str(pick(b,'title',default='')).strip()
def err_response(action,e,c):
    code=str(getattr(e,'code','') or '')
    if not code:
        msg=str(e)
        if 'AuthKeyDuplicatedError' in msg or 'authorization key' in msg.lower():code='auth_key_duplicated'
        elif 'NoActiveGroupCall' in msg or 'No active group call' in msg:code='no_active_call'
        else:code=f'{type(e).__name__}: {e}'
    return {'ok':False,'action':action,'error':code,'state':service.state(c)}
@app.on_event('startup')
async def startup():
    try:await service.ensure_ready()
    except Exception as e:log.exception('startup_failed: %s',e)
@app.on_event('shutdown')
async def shutdown():
    try:await service.close()
    except Exception:log.exception('shutdown_failed')
@app.get('/')
async def root():return {'ok':True,'service':'render-audio','ready':service.ready,'backend_error':service.backend_error}
@app.get('/ping',response_class=PlainTextResponse)
async def ping():return 'OK'
@app.get('/health')
async def health():return {'ok':True,'ready':service.ready,'backend_error':service.backend_error,'active_sessions':len(service.sessions)}
@app.get('/state/{chat_id}')
async def state(chat_id:int):return service.state(chat_id)
@app.post('/call/state')
async def call_state(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);b=await body(req);c=cid(b)
    try:return await service.call_state(c)
    except Exception as e:log.exception('call_state_failed chat_id=%s',c);return err_response('call_state',e,c)
@app.post('/meta')
async def meta(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);b=await body(req);c=cid(b)
    try:return await service.meta(c,stype(b),sid(b),title=title(b),source_chat_id=scid(b),source_message_id=smid(b))
    except Exception as e:log.exception('meta_failed chat_id=%s',c);return err_response('meta',e,c)
@app.post('/start')
async def start(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);b=await body(req);c=cid(b)
    try:return await service.start(c,stype(b),sid(b),title=title(b),duration=intval(b,'duration'),offset=intval(b,'offset'),source_chat_id=scid(b),source_message_id=smid(b))
    except Exception as e:log.exception('start_failed chat_id=%s',c);return err_response('start',e,c)
for action in ('pause','resume','stop','seek'):
    async def endpoint(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret'),_a=action):
        guard(x_keepalive_secret);b=await body(req);c=cid(b)
        try:
            if _a=='pause':return await service.pause(c)
            if _a=='resume':return await service.resume(c)
            if _a=='stop':return await service.stop(c)
            return await service.seek(c,intval(b,'delta'))
        except Exception as e:log.exception('%s_failed chat_id=%s',_a,c);return err_response(_a,e,c)
    endpoint.__name__=f'api_{action}'
    app.post(f'/{action}')(endpoint)
@app.post('/enqueue')
async def enqueue(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);b=await body(req);c=cid(b);return await service.enqueue(c,stype(b),sid(b),title=title(b),duration=intval(b,'duration'),requested_by=str(pick(b,'requestedBy','requested_by',default='')),auto_start=bool(pick(b,'autoStart','auto_start',default=False)),source_chat_id=scid(b),source_message_id=smid(b))
@app.post('/queue')
async def queue(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);return await service.queue_list(cid(await body(req)))
@app.post('/queue/list')
async def queue_list(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x_keepalive_secret')):return await queue(req,x_keepalive_secret)
@app.post('/queue/clear')
async def queue_clear(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);return await service.queue_clear(cid(await body(req)))
@app.post('/queue/skip')
async def queue_skip(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret);return await service.skip(cid(await body(req)))
@app.post('/queue/next')
async def queue_next(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):return await queue_skip(req,x_keepalive_secret)
if __name__=='__main__':
    import uvicorn
    uvicorn.run('app:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')),workers=1)
