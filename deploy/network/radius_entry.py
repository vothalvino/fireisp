#!/usr/bin/env python3
"""RADIUS container owns/restarts its own daemon; the web/agent never controls Docker."""
import os
import re
import signal
import time
from pathlib import Path

BASE = Path('/etc/freeradius/3.0')
GENERATED = Path('/var/lib/fireisp-radius')
URL = os.environ.get('NETWORK_RADIUS_URL', 'http://127.0.0.1:18000/network/radius').rstrip('/')
TOKEN = os.environ['NETWORK_RADIUS_TOKEN']
if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', TOKEN) or not re.fullmatch(r'https?://[a-zA-Z0-9.:-]+/network/radius', URL):
    raise SystemExit('Invalid bootstrap RADIUS configuration')

# No password/request debug logging. Standard package modules supply PAP/CHAP/MSCHAP.
for path in (BASE / 'sites-enabled').iterdir():
    path.unlink()
for path in (BASE / 'mods-enabled').iterdir():
    if path.name not in {'pap', 'chap', 'mschap', 'always', 'expiration', 'logintime', 'expr'}:
        path.unlink()
(BASE / 'clients.conf').write_text('$INCLUDE /var/lib/fireisp-radius/clients.conf\n')
(BASE / 'mods-enabled' / 'fireisp_rest').write_text('''rest fireisp_rest {
 connect_uri = "''' + URL + '''"
 authorize {
  uri = "${..connect_uri}/authorize/"
  method = post
  body = json
  force_to = json
  timeout = 5
 }
 accounting {
  uri = "${..connect_uri}/accounting/"
  method = post
  body = json
  timeout = 5
 }
 pool {
  start = 0
  min = 0
  max = 8
  spare = 1
  uses = 0
  retry_delay = 1
  lifetime = 0
  idle_timeout = 60
 }
}
''')
(BASE / 'mods-enabled' / 'fireisp_accounting').write_text('detail fireisp_accounting {\n filename = /var/log/freeradius/fireisp-accounting/%Y%m%d.detail\n permissions = 0600\n locking = yes\n}\n')
(BASE / 'mods-enabled' / 'fireisp_confirmed').write_text('files fireisp_confirmed {\n filename = /var/lib/fireisp-radius/entitlements\n}\n')
site = '''server fireisp {
 $INCLUDE /var/lib/fireisp-radius/listeners.conf
 authorize {
  update reply {
   Message-Authenticator := 0x00
  }
  update control {
   REST-HTTP-Header := "Authorization: Bearer ''' + TOKEN + '''"
  }
  fireisp_rest {
   fail = 1
  }
  if (fail) {
   fireisp_confirmed
   if (notfound || fail || reject) {
    reject
   }
  }
  elsif (reject || notfound || invalid) {
   reject
  }
  expiration
  chap
  pap
 }
 authenticate {
  Auth-Type PAP {
   pap
  }
  Auth-Type CHAP {
   chap
  }
 }
 accounting {
  update request {
   NAS-IP-Address := "%{Packet-Src-IP-Address}"
  }
  fireisp_accounting
  update control {
   REST-HTTP-Header := "Authorization: Bearer ''' + TOKEN + '''"
  }
  fireisp_rest
 }
}
'''
(BASE / 'sites-enabled' / 'fireisp').write_text(site)
os.chmod(BASE / 'sites-enabled' / 'fireisp', 0o640)
# freerad group can read the shared token configuration after privilege drop.
import grp
import pwd
from accounting_replay import AccountingReplay
from radius_daemon import replace_daemon
accounting_dir = Path('/var/log/freeradius/fireisp-accounting')
accounting_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chown(accounting_dir, pwd.getpwnam('freerad').pw_uid, grp.getgrnam('freerad').gr_gid)
os.chown(BASE / 'sites-enabled' / 'fireisp', 0, grp.getgrnam('freerad').gr_gid)
process = None
previous = None
stopping = False
replayer = AccountingReplay(URL, TOKEN)
import threading

def replay_loop():
    last_error_report = 0
    previous_error = False
    while not stopping:
        try:
            result = replayer.replay_once(limit=100)
        except Exception:
            # The journal/cursor is retained. Never log accounting payloads or HTTP bodies.
            result = {'error': True, 'backlog': True, 'processed': 0}
        if result.get('error') and time.monotonic() - last_error_report >= 60:
            print('Accounting replay pending: callback or journal validation failed; journal and checkpoint retained.', flush=True)
            last_error_report = time.monotonic()
        elif previous_error and not result.get('error'):
            print('Accounting replay resumed successfully.', flush=True)
        previous_error = bool(result.get('error'))
        # Each pass remains bounded; a healthy backlog should not inherit an artificial 50/s ceiling.
        time.sleep(0.1 if result.get('backlog') and result.get('processed') and not result.get('error') else 2)

threading.Thread(target=replay_loop, name='accounting-replay', daemon=True).start()

def stop(*args):
    global stopping
    stopping = True
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
while not stopping:
    try:
        generation = (GENERATED / 'generation').read_text()
        listeners = (GENERATED / 'listeners.conf').read_text()
    except FileNotFoundError:
        time.sleep(2)
        continue
    if generation != previous or (process and process.poll() is not None):
        process, accepted = replace_daemon(process, bool(listeners.strip()))
        if not accepted:
            print('RADIUS configuration rejected; the existing daemon was preserved when available.', flush=True)
            time.sleep(5)
            continue
        if process:
            print('FireISP RADIUS started on configured private WireGuard addresses.', flush=True)
        previous = generation
    time.sleep(2)
if process and process.poll() is None:
    process.terminate()
    process.wait(timeout=10)
