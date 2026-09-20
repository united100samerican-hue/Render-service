from __future__ import annotations
import asyncio,hashlib,json,mimetypes
from pathlib import Path
from typing import Any
import boto3
from botocore.config import Config as BotoConfig
from config import AUDIO_CACHE_ENABLED,R2_ACCESS_KEY_ID,R2_BUCKET,R2_ENDPOINT,R2_PRESIGN_SECONDS,R2_SECRET_ACCESS_KEY

class R2AudioCache:
 def __init__(self)->None:
  self.enabled=AUDIO_CACHE_ENABLED;self.bucket=R2_BUCKET;self.presign_seconds=R2_PRESIGN_SECONDS;self.client:Any|None=None
  if self.enabled:
   self.client=boto3.client("s3",endpoint_url=R2_ENDPOINT,aws_access_key_id=R2_ACCESS_KEY_ID,aws_secret_access_key=R2_SECRET_ACCESS_KEY,region_name="auto",config=BotoConfig(signature_version="s3v4",retries={"max_attempts":3,"mode":"standard"}))
 @staticmethod
 def key_for(*parts:object,suffix: str=".bin")->str:
  raw="|".join(str(x or "").strip() for x in parts);digest=hashlib.sha256(raw.encode()).hexdigest();safe=suffix if suffix.startswith(".") else "."+suffix
  return f"audio/{digest}{safe}"
 async def exists(self,key:str)->bool:
  if not self.enabled or not self.client or not key:return False
  def f():
   try:self.client.head_object(Bucket=self.bucket,Key=key);return True
   except Exception:return False
  return await asyncio.to_thread(f)
 async def upload(self,path:str|Path,key:str)->bool:
  if not self.enabled or not self.client or not key:return False
  p=Path(path)
  if not p.is_file() or p.stat().st_size<=0:return False
  ct=mimetypes.guess_type(p.name)[0] or "application/octet-stream"
  def f():
   try:self.client.upload_file(str(p),self.bucket,key,ExtraArgs={"ContentType":ct});return True
   except Exception:return False
  return await asyncio.to_thread(f)
 async def download(self,key:str,destination:str|Path)->bool:
  if not self.enabled or not self.client or not key:return False
  p=Path(destination);p.parent.mkdir(parents=True,exist_ok=True)
  def f():
   try:self.client.download_file(self.bucket,key,str(p));return p.is_file() and p.stat().st_size>0
   except Exception:
    try:p.unlink(missing_ok=True)
    except Exception:pass
    return False
  return await asyncio.to_thread(f)
 async def put_json(self,key:str,value:dict[str,Any])->bool:
  if not self.enabled or not self.client or not key:return False
  body=json.dumps(value,ensure_ascii=False,separators=(",",":")).encode()
  def f():
   try:self.client.put_object(Bucket=self.bucket,Key=key,Body=body,ContentType="application/json; charset=utf-8");return True
   except Exception:return False
  return await asyncio.to_thread(f)
 async def get_json(self,key:str)->dict[str,Any]|None:
  if not self.enabled or not self.client or not key:return None
  def f():
   try:
    body=self.client.get_object(Bucket=self.bucket,Key=key)["Body"].read()
    data=json.loads(body.decode("utf-8"))
    return data if isinstance(data,dict) else None
   except Exception:return None
  return await asyncio.to_thread(f)
 async def presigned_url(self,key:str)->str:
  if not self.enabled or not self.client or not key:return ""
  def f():
   try:return str(self.client.generate_presigned_url("get_object",Params={"Bucket":self.bucket,"Key":key},ExpiresIn=self.presign_seconds))
   except Exception:return ""
  return await asyncio.to_thread(f)
 async def delete(self,key:str)->None:
  if not self.enabled or not self.client or not key:return
  def f():
   try:self.client.delete_object(Bucket=self.bucket,Key=key)
   except Exception:pass
  await asyncio.to_thread(f)
