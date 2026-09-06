#!/usr/bin/env python3
"""Configuration diagnostic that never emits credential-bearing config dumps."""
import json
import os
import re
import subprocess
from pathlib import Path

secrets_to_hide = [os.environ.get('NETWORK_RADIUS_TOKEN', '')]
clients = Path('/var/lib/fireisp-radius/clients.conf')
if clients.exists():
    secrets_to_hide.extend(re.findall(r'^\s*secret\s*=\s*(\S+)', clients.read_text(), re.M))
entitlements = Path('/var/lib/fireisp-radius/entitlements')
if entitlements.exists():
    for match in re.findall(r'Cleartext-Password := ("(?:[^"\\]|\\.)*")', entitlements.read_text()):
        try:
            secrets_to_hide.append(json.loads(match))
        except ValueError:
            pass
result = subprocess.run(['freeradius', '-XC'], capture_output=True, text=True)
messages = []
for line in (result.stdout + result.stderr).splitlines():
    if re.search(r'error|failed|unknown|invalid|cannot|can.t|expected|unable', line, re.I) and not any(word in line.lower() for word in ('password', 'secret', 'http-header')):
        for secret in secrets_to_hide:
            if secret:
                line = line.replace(secret, '[protected]')
        messages.append(line[:500])
print(json.dumps({'valid': result.returncode == 0, 'messages': messages[-15:]}))
