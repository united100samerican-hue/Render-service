from __future__ import annotations
import logging, os
from typing import Any
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse
from service import service

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
log=logging.getLogger('audio_app')
app=FastAPI(title='Render Audio+URL Service',version='6.0')
SECRET=os.getenv('KEEPALIVE_SECRET','').strip()

def guard(value:str|None):
    if SECRET and (value or '').strip()!=SECRET: raise HTTPException(403,'forbidden')
async def body(req:Request)->dict[str,Any]:
    try:
        x=await req.json(); return x if isinstance(x,dict) else {}
    except Exception: return {}
def pick(b,*names,default=None):
    for n in names:
        if n in b and b[n] is not None: return b[n]
    return default
def cid(b):
    try:return int(pick(b,'chatId','chat_id',default=0))
    except:return 0
def stype(b):
    raw=str(pick(b,'sourceType','source_type',default='')).strip().lower().replace('-','_')
    if raw in {'link','url','youtube','yt','youtube_video'}: return 'url'
    if raw in {'audio','voice','music','song'}: return 'telegram_audio'
    if raw in {'video','clip','movie'}: return 'telegram_video'
    if raw in {'telegram','tg','telegram_file_id','telegram_document','document','file','media','file_id'}: return 'telegram_file_id'
    if raw=='telegram_message': return raw
    raise HTTPException(400,f'unsupported_source_type: {raw or "missing"}')
def sid(b): return str(pick(b,'sourceId','source_id','sourceUrl','source_url',default='')).strip()
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

@app.on_event('startup')
async def startup():
    try: await service.ensure_ready()
    except Exception: log.exception('startup_failed')
@app.on_event('shutdown')
async def shutdown():
    try: await service.close()
    except Exception: log.exception('shutdown_failed')
@app.get('/')
async def root(): return {'ok':True,'service':'render-audio','ready':service.ready}
@app.get('/ping',response_class=PlainTextResponse)
async def ping(): return 'OK'
@app.get('/health')
async def health(): return {'ok':True,'ready':service.ready,'backend_error':service.backend_error,'active_sessions':len(service.sessions)}
@app.get('/state/{chat_id}')
async def state(chat_id:int): return service.state(chat_id)

@app.post('/call/state')
async def call_state(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); b=await body(req); return await service.call_state(cid(b))

@app.post('/meta')
async def meta(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); b=await body(req); c=cid(b)
    try:return await service.meta(c,stype(b),sid(b),title=title(b),duration=intval(b,'duration'),source_chat_id=scid(b),source_message_id=smid(b))
    except Exception as e: log.exception('meta_failed'); return {'ok':False,'action':'meta','error':f'{type(e).__name__}: {e}','state':service.state(c)}

@app.post('/start')
async def start(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); b=await body(req); c=cid(b)
    try:return await service.start(c,stype(b),sid(b),title=title(b),duration=intval(b,'duration'),offset=intval(b,'offset'),source_chat_id=scid(b),source_message_id=smid(b))
    except Exception as e:
        log.exception('start_failed'); code=getattr(e,'code','')
        return {'ok':False,'action':'start','error':code or f'{type(e).__name__}: {e}','state':service.state(c)}

for action in ('pause','resume','stop','seek'):
    async def endpoint(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret'),_a=action):
        guard(x_keepalive_secret); b=await body(req); c=cid(b)
        try:
            if _a=='pause': return await service.pause(c)
            if _a=='resume': return await service.resume(c)
            if _a=='stop': return await service.stop(c)
            return await service.seek(c,intval(b,'delta'))
        except Exception as e: log.exception('%s_failed',_a); return {'ok':False,'action':_a,'error':f'{type(e).__name__}: {e}','state':service.state(c)}
    endpoint.__name__=f'api_{action}'
    app.post(f'/{action}')(endpoint)

@app.post('/enqueue')
async def enqueue(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); b=await body(req); c=cid(b)
    return await service.enqueue(c,stype(b),sid(b),title=title(b),duration=intval(b,'duration'),requested_by=str(pick(b,'requestedBy','requested_by',default='')),auto_start=bool(pick(b,'autoStart','auto_start',default=False)),source_chat_id=scid(b),source_message_id=smid(b))
@app.post('/queue')
async def queue(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); return await service.queue_list(cid(await body(req)))
@app.post('/queue/list')
async def queue_list(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')): return await queue(req,x_keepalive_secret)
@app.post('/queue/clear')
async def queue_clear(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); return await service.queue_clear(cid(await body(req)))
@app.post('/queue/skip')
async def queue_skip(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')):
    guard(x_keepalive_secret); return await service.skip(cid(await body(req)))
@app.post('/queue/next')
async def queue_next(req:Request,x_keepalive_secret:str|None=Header(default=None,alias='x-keepalive-secret')): return await queue_skip(req,x_keepalive_secret)

if __name__=='__main__':
    import uvicorn
    uvicorn.run('app:app',host='0.0.0.0',port=int(os.getenv('PORT','10000')),workers=1)
