#!/usr/bin/env python3
"""Reject HTTPS binaries without the Go fix for CVE-2026-56862."""
import json
import re
import sys


def verify_build_info(contents):
    toolchain = re.search(r'^go\s+go(\d+)\.(\d+)\.(\d+)\s*$', contents, re.MULTILINE)
    if not toolchain:
        raise ValueError('Caddy must identify a stable Go toolchain in its build information.')
    version = tuple(int(part) for part in toolchain.groups())
    # Require a supported release family with the TLS KeyUpdate fix. Pre-release,
    # development and older Go builds fail closed instead of relying on a tag.
    if version < (1, 26, 6):
        raise ValueError('Caddy needs Go 1.26.6 or newer to fix CVE-2026-56862.')
    module = re.search(r'^(?:mod|dep)\s+github\.com/caddyserver/caddy/v2\s+(v\d+\.\d+\.\d+|\(devel\))\s', contents, re.MULTILINE)
    if not module:
        raise ValueError('Caddy module was not found in the binary build information.')
    # Direct builds from verified source archives record main-module (devel).
    # Check the runtime here; `caddy version` and the source checksum identify
    # the release independently. Trimmed Go builds omit linker flag metadata.
    return {'caddy_module': module[1], 'go': '.'.join(toolchain.groups()), 'tls_keyupdate_fix': True}


if __name__ == '__main__':
    try:
        print(json.dumps(verify_build_info(sys.stdin.read())))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
