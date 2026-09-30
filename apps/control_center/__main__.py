"""Run locally with python3 -m apps.control_center. No paid service is activated."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import signal
import smtplib
import ssl
import sys
import threading

from . import VERSION
from .auth import login
from .bridge import Engine, safe_code
from .server import serve
from .store import Store

DEFAULT_HOME = Path.home()/".local/state/trader-brain/control-center"
REPO = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description="Trader Brain private paper control center")
    parser.add_argument("command", choices=["check", "doctor", "serve", "login-robinhood"])
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--origin")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--oauth-client-id")
    parser.add_argument("--tailnet-origin", help="Exact private HTTPS .ts.net origin; adds a loopback-only listener on 8766")
    args = parser.parse_args()
    os.umask(0o077)
    args.home.mkdir(parents=True,exist_ok=True,mode=0o700)
    if args.command == "login-robinhood":
        login(args.home/"robinhood-oauth.json", client_id=args.oauth_client_id)
        return 0
    store = Store(args.home/"control.sqlite3")
    engine = Engine(REPO,args.home,store)
    if args.command == "check":
        print(json.dumps({"status":"CONFIGURED_NOT_DEPLOYED","version":VERSION,
                          "paper_only":True,"broker_write_authority":False,"openai_api_used":False,
                          "baseline_modified":False,"paused":store.get("paused"),
                          "strategy_version":engine.runtime_version}))
        return 0
    if args.command == "doctor":
        checks = {}
        try:
            client=engine.client()
            checks["massive_stocks"] = {"ok": bool(client.previous_bar("SPY"))}
            today=datetime.now(engine.runtime.NY).date()
            chain=client.option_chain("SPY",today+timedelta(days=7),today+timedelta(days=21))
            now=datetime.now(engine.runtime.NY)
            fresh=sum(0 <= now.timestamp()-q["last_quote"]["last_updated"]/1e9 <= 180 for q in chain)
            checks["robinhood_options"]={"ok":bool(chain),"records":len(chain),"fresh_records":fresh,
                                         "note":"Old quotes outside market hours are not buy signals."}
        except Exception as exc: checks["data"]={"ok":False,"error":safe_code(exc)}
        try:
            sender=engine.runtime.require_env("TB_GMAIL_SENDER")
            recipient=engine.runtime.require_env("TB_GMAIL_RECIPIENT")
            if sender.casefold()!=recipient.casefold(): raise ValueError("self-send required")
            password="".join(engine.runtime.require_env("TB_GMAIL_APP_PASSWORD").split())
            with smtplib.SMTP_SSL("smtp.gmail.com",465,timeout=15,context=ssl.create_default_context()) as smtp:
                smtp.login(sender,password)
            checks["gmail"]={"ok":True,"test_email_sent":False}
        except Exception as exc: checks["gmail"]={"ok":False,"error":safe_code(exc)}
        print(json.dumps({"checks":checks,"mode":"paper_only","broker_write_authority":False}))
        return 0 if checks and all(c["ok"] for c in checks.values()) else 2
    token_path=args.home/"dashboard-token"
    if not token_path.exists():
        fd=os.open(token_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,"w") as out: out.write(secrets.token_urlsafe(32))
    if token_path.stat().st_mode & 0o077: raise ValueError("unsafe dashboard token permissions")
    token=token_path.read_text().strip()
    server=serve(engine,token,host=args.host,port=args.port,origin=args.origin,cert=args.cert,key=args.key)
    # Lifetime lock prevents two services with separate command queues sharing this app portfolio.
    import fcntl
    descriptor=os.open(args.home/"service.lock",os.O_CREAT|os.O_RDWR,0o600)
    try: fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise ValueError("control service already running")
    proxy = None
    if args.tailnet_origin:
        try:
            proxy=serve(engine,token,host="127.0.0.1",port=8766,
                        origin=args.tailnet_origin,tailnet_proxy=True)
        except Exception:
            server.server_close()
            os.close(descriptor)
            raise
    worker=threading.Thread(target=engine.run,name="paper-engine",daemon=True)
    worker.start()
    if proxy:
        threading.Thread(target=proxy.serve_forever,name="private-tailnet-http",daemon=True).start()
    def stop(signum, frame):
        engine.stop.set();engine.wake.set()
        threading.Thread(target=server.shutdown,daemon=True).start()
        if proxy: threading.Thread(target=proxy.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    print(json.dumps({"status":"CONTROL_LISTENING","pid":os.getpid(),"version":VERSION,
                      "origin":server.origin,"tailnet_origin":proxy.origin if proxy else None,"paper_only":True,"baseline_modified":False}),flush=True)
    try: server.serve_forever(poll_interval=.5)
    finally:
        engine.stop.set();engine.wake.set();worker.join(timeout=15)
        if proxy: proxy.shutdown();proxy.server_close()
        server.server_close();os.close(descriptor)
    return 0

if __name__ == "__main__":
    try: raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status":"NOT_READY","error":safe_code(exc)}),file=sys.stderr)
        raise SystemExit(2)
