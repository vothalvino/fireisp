"""Replay private FreeRADIUS detail records to the fixed accounting callback.

The journal is never removed or rewritten. A checkpoint is committed only after
HTTP 204; a crash in between intentionally retries the packet (API idempotency).
"""
from contextlib import contextmanager
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
import stat
import time
import urllib.parse
import urllib.request
import uuid

DIRECTORY = Path('/var/log/freeradius/fireisp-accounting')
CURSOR = '.replay-cursor.json'
LOCK = '.replay.lock'
MAX_BLOCK = 32768
MAX_LINE = 4096
MAX_FILES = 4096
MAX_CURSOR = 2 * 1024 * 1024
NAME = re.compile(r'[0-9]{8}\.detail\Z')
HEADER = re.compile(r'(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) +[0-9]{1,2} [0-9]{2}:[0-9]{2}:[0-9]{2} [0-9]{4}\Z')
ATTRIBUTE = re.compile(r'[ \t]+([A-Za-z][A-Za-z0-9_-]{0,127})[ \t]*=[ \t]*(.*)\Z')
NUMERIC = {'Acct-Input-Octets', 'Acct-Output-Octets', 'Acct-Input-Gigawords', 'Acct-Output-Gigawords', 'Acct-Session-Time', 'Acct-Delay-Time', 'Timestamp'}
IPS = {'NAS-IP-Address', 'Packet-Src-IP-Address', 'Framed-IP-Address'}
STRINGS = {'User-Name': 100, 'Acct-Session-Id': 128, 'Acct-Status-Type': 32, 'Acct-Terminate-Cause': 128}
ALLOWED = NUMERIC | IPS | set(STRINGS)
STATUSES = {'Start', 'Stop', 'Interim-Update', 'Accounting-On', 'Accounting-Off'}


def _value(raw):
    if raw.startswith('"'):
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError('Invalid quoted detail value.')
    else:
        if not raw or '"' in raw or '\\' in raw or any(char.isspace() for char in raw):
            raise ValueError('Invalid detail value.')
        value = raw
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Control characters are not allowed in detail values.')
    return value


def parse_detail_block(block):
    """Decode only the attributes accepted by the accounting endpoint."""
    if not isinstance(block, bytes) or not block or len(block) > MAX_BLOCK:
        raise ValueError('Invalid detail block size.')
    lines = block.decode('utf-8', errors='strict').splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2 or len(lines[0]) > 80 or not HEADER.fullmatch(lines[0]):
        raise ValueError('Invalid detail header.')
    result = {}
    for line in lines[1:]:
        if len(line.encode('utf-8')) > MAX_LINE:
            raise ValueError('Detail line exceeds its bound.')
        match = ATTRIBUTE.fullmatch(line)
        if not match:
            raise ValueError('Malformed detail attribute.')
        name, raw = match.groups()
        # Unknown fields (including passwords, CHAP and HTTP headers) never leave
        # the protected journal. Their grammar is still checked before proceeding.
        value = _value(raw.strip())
        if name not in ALLOWED:
            continue
        if name in result or (name == 'Timestamp' and 'FireISP-Journal-Timestamp' in result):
            raise ValueError('Duplicate accounting attribute.')
        if name in NUMERIC:
            if not re.fullmatch(r'[0-9]{1,20}', value):
                raise ValueError('Invalid accounting counter.')
            number = int(value)
            if number > 2**63 - 1:
                raise ValueError('Accounting counter exceeds its bound.')
            if name == 'Timestamp':
                if number <= 0 or number > 253402300799:
                    raise ValueError('Invalid journal timestamp.')
                result['FireISP-Journal-Timestamp'] = number
            else:
                result[name] = number
        elif name in IPS:
            result[name] = str(ipaddress.IPv4Address(value))
        else:
            if not value or len(value) > STRINGS[name]:
                raise ValueError('Invalid accounting string length.')
            result[name] = value
    status = result.get('Acct-Status-Type')
    if status not in STATUSES or 'FireISP-Journal-Timestamp' not in result:
        raise ValueError('Required accounting fields are missing.')
    if not (result.get('Packet-Src-IP-Address') or result.get('NAS-IP-Address')):
        raise ValueError('Accounting router is missing.')
    if status in {'Start', 'Stop', 'Interim-Update'} and not (result.get('User-Name') and result.get('Acct-Session-Id')):
        raise ValueError('Accounting session identity is missing.')
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _regular(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Journal state must be a regular file.')
    return info


@contextmanager
def _opened(directory_fd, name, flags, mode=0o600):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=directory_fd)
    try:
        _regular(fd)
        yield fd
    finally:
        os.close(fd)


def _anchor(fd, offset):
    return hashlib.sha256(os.pread(fd, min(128, offset), max(0, offset - 128))).hexdigest()


class AccountingReplay:
    def __init__(self, url, token, *, _directory=None, _opener=None):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path != '/network/radius' or parsed.query or parsed.fragment
                or not re.fullmatch(r'https?://[A-Za-z0-9.:-]+/network/radius', url)):
            raise ValueError('Invalid fixed RADIUS callback URL.')
        try:
            parsed.port
        except ValueError:
            raise ValueError('Invalid fixed RADIUS callback port.') from None
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
            raise ValueError('Invalid RADIUS callback credential.')
        self.url = url + '/accounting/'
        self.token = token
        self.directory = Path(_directory) if _directory is not None else DIRECTORY
        self.opener = _opener if _opener is not None else urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def _load(self, directory_fd):
        try:
            with _opened(directory_fd, CURSOR, os.O_RDONLY) as fd:
                if os.fstat(fd).st_size > MAX_CURSOR:
                    raise ValueError('Checkpoint exceeds its bound.')
                data = os.read(fd, MAX_CURSOR + 1)
        except FileNotFoundError:
            return {'format': 1, 'files': {}}
        state = json.loads(data)
        if not isinstance(state, dict) or state.get('format') != 1 or not isinstance(state.get('files'), dict) or len(state['files']) > MAX_FILES:
            raise ValueError('Invalid replay checkpoint.')
        for name, record in state['files'].items():
            if (not NAME.fullmatch(name) or not isinstance(record, dict)
                    or any(type(record.get(key)) is not int or record[key] < 0 for key in ('dev', 'ino', 'offset'))
                    or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('anchor', '')))):
                raise ValueError('Invalid replay checkpoint entry.')
        return state

    def _save(self, directory_fd, state):
        data = json.dumps(state, sort_keys=True, separators=(',', ':')).encode()
        if len(data) > MAX_CURSOR:
            raise ValueError('Checkpoint exceeds its bound.')
        temporary = '.replay-cursor.' + uuid.uuid4().hex + '.tmp'
        try:
            with _opened(directory_fd, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL) as fd:
                os.fchmod(fd, 0o600)
                with os.fdopen(os.dup(fd), 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            os.replace(temporary, CURSOR, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    @staticmethod
    def _block(stream):
        block = bytearray()
        while True:
            line = stream.readline(MAX_LINE + 2)
            if not line:
                return None, bool(block)
            if len(line) > MAX_LINE or len(block) + len(line) > MAX_BLOCK:
                raise ValueError('Detail block exceeds its bound.')
            if not line.endswith(b'\n'):
                return None, True
            if not line.strip():
                if block:
                    return bytes(block), False
                continue
            block.extend(line)

    def replay_once(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Replay limit must be between 1 and 1000.')
        result = {'processed': 0, 'backlog': False, 'error': False, 'incomplete': 0}
        directory_fd = None
        try:
            directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            with _opened(directory_fd, LOCK, os.O_RDWR | os.O_CREAT) as lock_fd:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result['backlog'] = True
                    return result
                state = self._load(directory_fd)
                with os.scandir(directory_fd) as entries:
                    names = []
                    for entry in entries:
                        if NAME.fullmatch(entry.name):
                            names.append(entry.name)
                        if len(names) > MAX_FILES:
                            raise ValueError('Journal inventory exceeds its bound.')
                deadline = time.monotonic() + 15
                for name in sorted(names):
                    if result['processed'] >= limit or time.monotonic() >= deadline:
                        result['backlog'] = True
                        break
                    with _opened(directory_fd, name, os.O_RDONLY) as fd:
                        info = os.fstat(fd)
                        saved = state['files'].get(name, {})
                        offset = saved.get('offset', 0)
                        if (saved.get('dev') != info.st_dev or saved.get('ino') != info.st_ino
                                or offset > info.st_size or saved.get('anchor') != _anchor(fd, offset)):
                            offset = 0
                        with os.fdopen(os.dup(fd), 'rb') as stream:
                            stream.seek(offset)
                            while stream.tell() < os.fstat(fd).st_size:
                                if result['processed'] >= limit or time.monotonic() >= deadline:
                                    result['backlog'] = True
                                    break
                                block, incomplete = self._block(stream)
                                if block is None:
                                    result['incomplete'] += int(incomplete)
                                    result['backlog'] |= incomplete
                                    break
                                payload = parse_detail_block(block)
                                request = urllib.request.Request(self.url, data=json.dumps(payload, separators=(',', ':')).encode(), method='POST', headers={
                                    'Authorization': 'Bearer ' + self.token,
                                    'Content-Type': 'application/json',
                                })
                                with self.opener.open(request, timeout=5) as response:
                                    if response.status != 204:
                                        raise ValueError('Accounting callback did not confirm processing.')
                                offset = stream.tell()
                                state['files'][name] = {'dev': info.st_dev, 'ino': info.st_ino, 'offset': offset, 'anchor': _anchor(fd, offset)}
                                self._save(directory_fd, state)
                                result['processed'] += 1
            return result
        except Exception:
            # No payload, endpoint, credentials, private path, or exception text is
            # included in diagnostics. The untouched journal remains retryable.
            result['error'] = True
            result['backlog'] = True
            return result
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
