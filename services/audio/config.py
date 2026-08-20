from __future__ import annotations
import os
API_ID=int(os.getenv('API_ID','0') or 0)
API_HASH=os.getenv('API_HASH','').strip()
AUDIO_SESSION_STRING=os.getenv('AUDIO_SESSION_STRING','').strip()
SESSION_STRING=AUDIO_SESSION_STRING or os.getenv('SESSION_STRING','').strip()
BOT_TOKEN=os.getenv('BOT_TOKEN','').strip()
KEEPALIVE_SECRET=os.getenv('KEEPALIVE_SECRET','').strip()
