#!/usr/bin/env python3
"""Root helper. Fixed network operations, deterministic names, no shell or arbitrary paths."""
import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get('NETWORK_STATE_DIR', '/var/lib/fireisp-network'))
RADIUS = Path(os.environ.get('NETWORK_RADIUS_CONFIG_DIR', '/var/lib/fireisp-radius'))
SOCKET = Path(os.environ.get('NETWORK_AGENT_SOCKET', '/run/fireisp-network/agent.sock'))
LABS = {}
MAX_ENTITLEMENT_ENTRIES = 25_000
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_ENTITLEMENTS_BYTES = 16 * 1024 * 1024


class Rejected(Exception):
    pass


def command(*args, input=None, check=True, timeout=20):
    result = subprocess.run(list(args), input=input, text=True, capture_output=True, timeout=timeout, shell=False)
    if check and result.returncode:
        raise Rejected('Falló una operación de red del agente. Verifique capacidades NET_ADMIN/NET_RAW, dispositivos tun/ppp y servicios del instalador.')
    return result


def config(router_id):
    if type(router_id) is not int or not 1 <= router_id <= 4095:
        raise Rejected('ID de router fuera de la reserva permitida.')
    start = int(ipaddress.ip_address('10.253.0.0')) + router_id * 4
    return {'id': router_id, 'wg': f'fi{router_id}wg', 'tap': f'fi{router_id}tap', 'ppp': f'fi{router_id}ppp', 'server': str(ipaddress.ip_address(start + 1)), 'router': str(ipaddress.ip_address(start + 2)), 'server_port': 50000 + router_id, 'router_port': 55000 + router_id, 'gateway': str(ipaddress.ip_address(int(ipaddress.ip_address('10.254.0.0')) + router_id * 8 + 1))}


def atomic(path, value, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)
    os.chmod(temp, mode)
    temp.replace(path)


def state_path(router_id):
    return STATE / f'router-{router_id}.json'


def read_state(router_id):
    try:
        return json.loads(state_path(router_id).read_text())
    except FileNotFoundError:
        raise Rejected('Primero prepare el router mediante un trabajo aprobado.')


def prepare(router_id):
    cfg = config(router_id)
    path = state_path(router_id)
    if not path.exists():
        private = command('wg', 'genkey').stdout.strip()
        public = command('wg', 'pubkey', input=private).stdout.strip()
        atomic(path, json.dumps({'public_key': public, 'private_key': private, 'configured': False}))
    return {'public_key': read_state(router_id)['public_key'], **cfg}


def key(value):
    try:
        if not isinstance(value, str) or len(base64.b64decode(value, validate=True)) != 32:
            raise ValueError
    except ValueError:
        raise Rejected('Clave pública WireGuard inválida.')
    return value


def owned_link(cfg):
    result = command('ip', '-j', 'link', 'show', 'dev', cfg['wg'], check=False)
    if result.returncode:
        return False
    links = json.loads(result.stdout)
    if not links or links[0].get('ifalias') != f'fireisp:{cfg["id"]}':
        raise Rejected('La interfaz propuesta ya existe y no pertenece a FireISP.')
    return True


def write_radius():
    entries = []
    listeners = []
    for path in sorted(STATE.glob('router-*.json')):
        state = json.loads(path.read_text())
        if not state.get('configured'):
            continue
        cfg = config(int(path.stem.split('-')[1]))
        entries.append(f'client fi{cfg["id"]} {{\n ipaddr = {cfg["router"]}\n secret = {state["radius_secret"]}\n require_message_authenticator = yes\n}}\n')
        for kind, port in [('auth', 1812), ('acct', 1813)]:
            listeners.append(f'listen {{\n type = {kind}\n ipaddr = {cfg["server"]}\n port = {port}\n}}\n')
    # Service files are mounted only into the RADIUS container, never the web application.
    atomic(RADIUS / 'clients.conf', '\n'.join(entries), 0o644)
    atomic(RADIUS / 'listeners.conf', '\n'.join(listeners), 0o644)
    atomic(RADIUS / 'generation', secrets.token_hex(16), 0o644)


def firewall_rules(cfg, endpoint, remove=False):
    rules = [(['-p', 'udp', '-s', endpoint, '--dport', str(cfg['server_port'])], 'udp')]
    for protocol in ('icmp', 'gre'):
        rules.append((['-i', cfg['wg'], '-s', cfg['router'] + '/32', '-p', protocol], protocol))
    rules.append((['-i', cfg['wg'], '-s', cfg['router'] + '/32', '-p', 'udp', '-m', 'multiport', '--dports', '1812,1813'], 'radius'))
    for fields, suffix in rules:
        args = fields + ['-m', 'comment', '--comment', f'fireisp:{cfg["id"]}:host-{suffix}', '-j', 'ACCEPT']
        exists = command('iptables', '-w', '5', '-C', 'INPUT', *args, check=False).returncode == 0
        if remove and exists:
            command('iptables', '-w', '5', '-D', 'INPUT', *args)
        elif not remove and not exists:
            command('iptables', '-w', '5', '-I', 'INPUT', '1', *args)


def configure(router_id, peer_public_key, remote_endpoint, radius_secret):
    cfg = config(router_id)
    state = read_state(router_id)
    peer_public_key = key(peer_public_key)
    remote_endpoint = str(ipaddress.IPv4Address(remote_endpoint))
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', radius_secret):
        raise Rejected('Secreto RADIUS inválido.')
    state.update(peer_public_key=peer_public_key, remote_endpoint=remote_endpoint, radius_secret=radius_secret)
    atomic(state_path(router_id), json.dumps(state))
    if not owned_link(cfg):
        command('ip', 'link', 'add', cfg['wg'], 'type', 'wireguard')
        command('ip', 'link', 'set', cfg['wg'], 'alias', f'fireisp:{router_id}')
    private_path = STATE / f'router-{router_id}.key'
    atomic(private_path, state['private_key'] + '\n')
    command('wg', 'set', cfg['wg'], 'listen-port', str(cfg['server_port']), 'private-key', str(private_path), 'peer', peer_public_key, 'allowed-ips', cfg['router'] + '/32', 'endpoint', f'{remote_endpoint}:{cfg["router_port"]}', 'persistent-keepalive', '25')
    command('ip', 'address', 'replace', cfg['server'] + '/30', 'dev', cfg['wg'])
    command('ip', 'link', 'set', cfg['wg'], 'mtu', '1380', 'up')
    firewall_rules(cfg, remote_endpoint)
    state.update(peer_public_key=peer_public_key, remote_endpoint=remote_endpoint, radius_secret=radius_secret, configured=True)
    atomic(state_path(router_id), json.dumps(state))
    write_radius()
    return {'configured': True, 'server_address': cfg['server'], 'listen_port': cfg['server_port']}


def status(router_id):
    cfg = config(router_id)
    if not owned_link(cfg):
        return {'configured': False, 'handshake_recent': False}
    rows = command('wg', 'show', cfg['wg'], 'latest-handshakes').stdout.splitlines()
    latest = max((int(row.split()[1]) for row in rows if len(row.split()) == 2), default=0)
    age = int(time.time()) - latest if latest else None
    transfers = command('wg', 'show', cfg['wg'], 'transfer').stdout.splitlines()
    rx = sum(int(row.split()[1]) for row in transfers if len(row.split()) == 3)
    tx = sum(int(row.split()[2]) for row in transfers if len(row.split()) == 3)
    return {'configured': True, 'handshake_recent': age is not None and age < 180, 'last_handshake_age_seconds': age, 'rx_bytes': rx, 'tx_bytes': tx}


def lab_stop(router_id):
    config(router_id)  # Validate the resource namespace before touching process state.
    processes = LABS.pop(router_id, [])
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
    # TAP is non-persistent and disappears with the EoIP process. Never remove unowned interfaces.
    for name in (f'router-{router_id}.ppp-options',):
        (STATE / name).unlink(missing_ok=True)
    return {'stopped': True}


def ppp_failure(router_id):
    path = STATE / f'router-{router_id}.ppp-log'
    text = path.read_text(errors='replace').lower() if path.exists() else ''
    stages = [('unrecognized option', 'opción pppd no compatible'), ('couldn\'t load plugin', 'plugin PPPoE no disponible'), ('authentication failed', 'autenticación RADIUS rechazada'), ('timeout waiting for pado', 'sin respuesta PADO del servidor PPPoE'), ('timeout waiting for pads', 'sin confirmación PADS del servidor PPPoE'), ('operation not permitted', 'capacidad o dispositivo del contenedor insuficiente'), ('no such device', 'dispositivo PPP no disponible')]
    reason = next((label for pattern, label in stages if pattern in text), 'cliente PPP terminó sin IP')
    try:
        metrics = json.loads((STATE / f'router-{router_id}.eoip-status').read_text())
    except (FileNotFoundError, ValueError):
        metrics = {}
    return f'PPPoE: {reason}; EoIP transmitidos={metrics.get("tx", 0)}, recibidos={metrics.get("rx", 0)}.'


def lab_start(router_id, username, password, service_name):
    cfg = config(router_id)
    if not status(router_id).get('handshake_recent'):
        raise Rejected('El enlace privado no tiene un handshake reciente.')
    if not re.fullmatch(fr'fi{router_id}-lab-[a-f0-9]{{8}}', username) or not re.fullmatch(r'[A-Za-z0-9_-]{24,100}', password) or service_name != f'fireisp-lab-{router_id}':
        raise Rejected('Solo se permite una credencial temporal del laboratorio de este router.')
    lab_stop(router_id)
    if command('ip', 'link', 'show', cfg['tap'], check=False).returncode == 0 or command('ip', 'link', 'show', cfg['ppp'], check=False).returncode == 0:
        raise Rejected('Ya existe una interfaz con el nombre reservado del cliente de laboratorio.')
    eoip = subprocess.Popen([sys.executable, str(Path(__file__).with_name('eoip.py')), str(router_id)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    LABS[router_id] = [eoip]
    for _ in range(30):
        if command('ip', 'link', 'show', cfg['tap'], check=False).returncode == 0:
            break
        if eoip.poll() is not None:
            raise Rejected('No pudo iniciarse EoIP; verifique /dev/net/tun y NET_RAW.')
        time.sleep(0.2)
    options = '\n'.join(['plugin rp-pppoe.so', f'nic-{cfg["tap"]}', f'rp_pppoe_service {service_name}', f'user {username}', f'password {password}', f'ifname {cfg["ppp"]}', 'noauth', 'noipdefault', 'nodefaultroute', 'hide-password', 'mtu 1280', 'mru 1280', 'lcp-echo-interval 5', 'lcp-echo-failure 3', 'maxfail 1', 'nodetach', f'logfile {STATE / ("router-" + str(router_id) + ".ppp-log")}', 'ipparam fireisp-lab']) + '\n'
    path = STATE / f'router-{router_id}.ppp-options'
    atomic(path, options)
    log_path = STATE / f'router-{router_id}.ppp-log'
    atomic(log_path, '')
    with log_path.open('a') as log:
        ppp = subprocess.Popen(['pppd', 'file', str(path)], stdout=log, stderr=log)
    LABS[router_id].append(ppp)
    addresses = []
    for _ in range(45):
        result = command('ip', '-j', 'address', 'show', 'dev', cfg['ppp'], check=False)
        if result.returncode == 0:
            addresses = [addr for link in json.loads(result.stdout) for addr in link.get('addr_info', []) if addr.get('family') == 'inet']
            if addresses:
                break
        if ppp.poll() is not None:
            raise Rejected(ppp_failure(router_id))
        time.sleep(1)
    if not addresses:
        raise Rejected(ppp_failure(router_id))
    ping = command('ping', '-I', cfg['ppp'], '-c', '3', '-W', '2', cfg['gateway'], check=False, timeout=10)
    return {'ppp_up': True, 'framed_ip': addresses[0].get('local'), 'gateway_ping': ping.returncode == 0, 'layer2': 'EoIP over private WireGuard', 'internet_tested': False}


def disconnect(router_id, username, session_id, port):
    cfg = config(router_id)
    state = read_state(router_id)
    if not isinstance(username, str) or not 1 <= len(username) <= 100 or any(ord(c) < 32 for c in username):
        raise Rejected('Usuario inválido.')
    if not isinstance(session_id, str) or not 1 <= len(session_id) <= 128 or any(ord(c) < 32 for c in session_id):
        raise Rejected('Sesión inválida.')
    if type(port) is not int or not 1024 <= port <= 65535:
        raise Rejected('Puerto de desconexión inválido.')
    def attribute(kind, value):
        raw = value.encode()
        return bytes((kind, len(raw) + 2)) + raw
    attrs = attribute(1, username) + attribute(44, session_id) + bytes((4, 6)) + socket.inet_aton(cfg['router'])
    ident = secrets.randbelow(256)
    header = struct.pack('!BBH', 40, ident, 20 + len(attrs))
    secret = state['radius_secret'].encode()
    authenticator = hashlib.md5(header + bytes(16) + attrs + secret).digest()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.bind((cfg['server'], 0))
        client.settimeout(5)
        client.sendto(header + authenticator + attrs, (cfg['router'], port))
        try:
            response, sender = client.recvfrom(4096)
        except socket.timeout:
            return {'ack': False, 'reason': 'timeout'}
    if sender[0] != cfg['router'] or len(response) < 20:
        return {'ack': False, 'reason': 'invalid response'}
    code, received_id, length = struct.unpack('!BBH', response[:4])
    valid = received_id == ident and length == len(response) and secrets.compare_digest(response[4:20], hashlib.md5(response[:4] + authenticator + response[20:] + secret).digest())
    return {'ack': valid and code == 41, 'nak': valid and code == 42}


def radius_listener_health():
    required = set()
    for path in STATE.glob('router-*.json'):
        state = json.loads(path.read_text())
        if state.get('configured'):
            cfg = config(int(path.stem.split('-')[1]))
            required.update({(cfg['server'], 1812), (cfg['server'], 1813)})
    observed = set()
    for line in Path('/proc/net/udp').read_text().splitlines()[1:]:
        local = line.split()[1]
        address, port = local.split(':')
        observed.add((socket.inet_ntoa(struct.pack('<I', int(address, 16))), int(port, 16)))
    return {'radius_ready': bool(required) and required.issubset(observed), 'required_listeners': len(required), 'observed_listeners': len(required & observed)}


def sync_entitlements(router_id, entries):
    if router_id != 1 or not isinstance(entries, list) or len(entries) > MAX_ENTITLEMENT_ENTRIES:
        raise Rejected('Instantánea de autorizaciones inválida.')
    users = []
    seen = set()
    rendered_bytes = 0
    from datetime import datetime
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {'username', 'password', 'router_id', 'upload_mbps', 'download_mbps', 'expires_at'}:
            raise Rejected('Campos de autorización no permitidos.')
        cfg = config(entry['router_id'])
        username, password = entry['username'], entry['password']
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}', username) or username in seen:
            raise Rejected('Usuario de autorización inválido o duplicado.')
        if not isinstance(password, str) or not 1 <= len(password) <= 128 or any(ord(c) < 32 for c in password):
            raise Rejected('Contraseña de autorización inválida.')
        for field in ('upload_mbps', 'download_mbps'):
            if type(entry[field]) is not int or not 1 <= entry[field] <= 100000:
                raise Rejected('Velocidad de autorización inválida.')
        seen.add(username)
        checks = f'Packet-Src-IP-Address == {cfg["router"]}, Cleartext-Password := ' + json.dumps(password, ensure_ascii=False)
        if entry['expires_at']:
            expires = datetime.fromisoformat(entry['expires_at'])
            # FreeRADIUS expiration module evaluates this independently during API outages.
            checks += ', Expiration := "' + expires.strftime('%b %d %Y %H:%M:%S UTC') + '"'
        rendered = json.dumps(username) + ' ' + checks + '\n' + f' Mikrotik-Rate-Limit := "{entry["upload_mbps"]}M/{entry["download_mbps"]}M",\n Acct-Interim-Interval := 60\n'
        rendered_bytes += len(rendered.encode('utf-8')) + bool(users)
        if rendered_bytes > MAX_ENTITLEMENTS_BYTES:
            raise Rejected('El archivo de autorizaciones excede el límite.')
        users.append(rendered)
    text = '\n'.join(users)
    path = RADIUS / 'entitlements'
    previous = None
    if path.exists():
        with path.open('rb') as stream:
            previous = stream.read(MAX_ENTITLEMENTS_BYTES + 1)
        if len(previous) > MAX_ENTITLEMENTS_BYTES:
            raise Rejected('El archivo de autorizaciones anterior excede el límite; se conservó sin cambios.')
    if previous != text.encode('utf-8'):
        atomic(path, text, 0o644)
        atomic(RADIUS / 'generation', secrets.token_hex(16), 0o644)
    atomic(RADIUS / 'entitlements-status.json', json.dumps({'confirmed_at': int(time.time()), 'accounts': len(entries)}), 0o644)
    return {'confirmed_accounts': len(entries), **radius_listener_health()}


def remove(router_id):
    cfg = config(router_id)
    lab_stop(router_id)
    try:
        state = read_state(router_id)
    except Rejected:
        state = {}
    if state.get('remote_endpoint'):
        firewall_rules(cfg, state['remote_endpoint'], remove=True)
    if owned_link(cfg):
        command('ip', 'link', 'delete', cfg['wg'])
    state_path(router_id).unlink(missing_ok=True)
    (STATE / f'router-{router_id}.key').unlink(missing_ok=True)
    write_radius()
    return {'removed': True}


OPERATIONS = {'prepare': (prepare, set()), 'configure': (configure, {'peer_public_key', 'remote_endpoint', 'radius_secret'}), 'status': (status, set()), 'remove': (remove, set()), 'lab_start': (lab_start, {'username', 'password', 'service_name'}), 'lab_stop': (lab_stop, set()), 'disconnect': (disconnect, {'username', 'session_id', 'port'}), 'sync_entitlements': (sync_entitlements, {'entries'})}


def dispatch(request):
    if not isinstance(request, dict) or request.get('operation') not in OPERATIONS:
        raise Rejected('Operación no permitida.')
    operation, fields = OPERATIONS[request['operation']]
    if set(request) != {'operation', 'router_id'} | fields:
        raise Rejected('Parámetros no permitidos.')
    config(request['router_id'])
    return operation(**{key: value for key, value in request.items() if key != 'operation'})


def read_request(stream):
    line = stream.readline(MAX_REQUEST_BYTES + 1)
    if len(line) > MAX_REQUEST_BYTES or not line.endswith(b'\n'):
        raise Rejected('Solicitud excede el límite.')
    return json.loads(line)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        allowed = {int(value) for value in os.environ.get('NETWORK_WORKER_UIDS', '1000').split(',')}
        if uid not in allowed:
            self.wfile.write(b'{"ok":false,"error":"Proceso no autorizado."}\n')
            return
        try:
            self.request.settimeout(5)
            result = dispatch(read_request(self.rfile))
            response = {'ok': True, 'result': result}
        except Rejected as exc:
            response = {'ok': False, 'error': str(exc)}
        except Exception:
            response = {'ok': False, 'error': 'El agente rechazó la solicitud o falló una dependencia del instalador.'}
        self.wfile.write(json.dumps(response).encode() + b'\n')


def main():
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    RADIUS.mkdir(parents=True, exist_ok=True)
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    for path in STATE.glob('router-*.json'):
        state = json.loads(path.read_text())
        if state.get('configured'):
            configure(int(path.stem.split('-')[1]), state['peer_public_key'], state['remote_endpoint'], state['radius_secret'])
    write_radius()
    if not (RADIUS / 'entitlements').exists():
        atomic(RADIUS / 'entitlements', '', 0o644)
    with socketserver.UnixStreamServer(str(SOCKET), Handler) as server:
        os.chmod(SOCKET, 0o660)
        os.chown(SOCKET, 0, int(os.environ.get('NETWORK_WORKER_GID', '1000')))
        print('FireISP restricted network agent ready', flush=True)
        server.serve_forever()


if __name__ == '__main__':
    main()
