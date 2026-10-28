#!/usr/bin/env python3
"""Explicit initialization, one-shot worker and replacement web entry point."""
import argparse,os
from outreach_v2.db import Database

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['init','serve','tick']);p.add_argument('--port',type=int,default=int(os.environ.get('PORT','8020')));p.add_argument('--host',default='127.0.0.1');a=p.parse_args()
    db=Database()
    if a.command=='init':db.initialize()
    elif a.command=='tick':
        from outreach_v2.worker import tick
        result=tick(db);print(result)
        if result['status']!='COMPLETED':raise SystemExit(1)
    else:
        import uvicorn
        from outreach_v2.app import create_app
        uvicorn.run(create_app(db),host=a.host,port=a.port)
if __name__=='__main__':main()
