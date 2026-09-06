"""Enroll an execution node and maintain its private connection to the main server.

Only OpenSSH handles operator authentication. The installer never reads, stores,
or puts an SSH password on a command line. Enrollment secrets travel over that
authenticated connection and are written only to owner-readable files.
"""
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlsplit, urlunsplit


ROLES = frozenset({'web', 'worker', 'billing', 'fiscal', 'scheduler', 'network'})
COMMON_ENVIRONMENT = frozenset({
    'SECRET_KEY', 'ENCRYPTION_KEY', 'DATABASE_URL', 'REDIS_URL', 'ALLOWED_HOSTS',
    'CSRF_TRUSTED_ORIGINS', 'FIREISP_VERSION',
})
NETWORK_ENVIRONMENT = frozenset({'NETWORK_RADIUS_TOKEN', 'NETWORK_PUBLIC_ENDPOINT', 'NETWORK_RADIUS_URL'})
MAIN_PORTS = {'database': 15432, 'redis': 16379, 'web': 18000}
PREFERRED_PORTS = {'database': 25432, 'redis': 26379, 'web': 28000}


def validate_host(main_host):
    if not isinstance(main_host, str) or not main_host or len(main_host) > 253:
        raise ValueError('Enter the main server hostname or IP address without a URL or path.')
    try:
        ipaddress.ip_address(main_host)
    except ValueError:
        labels = main_host.rstrip('.').split('.')
        if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
            raise ValueError('Enter the main server hostname or IP address without a URL or path.') from None
    if '%' in main_host:
        raise ValueError('Use a hostname without an IPv6 scope identifier.')
    return main_host


def validate_identity(main_host, ssh_port, admin_user, node_id, roles, network_endpoint=''):
    validate_host(main_host)
    if not isinstance(ssh_port, int) or isinstance(ssh_port, bool) or not 1 <= ssh_port <= 65535:
        raise ValueError('Use a valid SSH port and a hostname without an IPv6 scope identifier.')
    if not isinstance(admin_user, str) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', admin_user):
        raise ValueError('Enter a valid administrator SSH username.')
    if not isinstance(node_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,29}', node_id):
        raise ValueError('Node ID must contain at most 30 lowercase letters, digits, or hyphens.')
    if not isinstance(roles, (tuple, list, set, frozenset)) or not roles or any(role not in ROLES for role in roles):
        raise ValueError('Select at least one supported execution module.')
    if len(roles) != len(set(roles)):
        raise ValueError('Select each execution module only once.')
    if 'network' in roles:
        try:
            address = ipaddress.ip_address(network_endpoint)
            if address.version != 4 or not address.is_global:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('The network module requires this node’s public IPv4 address.') from None


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid():
        raise ValueError('The connection directory must be owned by the installer account and cannot be a symlink.')
    path.chmod(0o700)


def private_file(path, *, maximum=131072, owners=None):
    metadata = path.lstat()
    allowed_owners = {os.geteuid()} if owners is None else owners
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in allowed_owners
            or metadata.st_mode & 0o077 or metadata.st_size > maximum):
        raise ValueError('A connection file must be a regular owner-only file; check ownership and chmod 600.')


def write_private(path, content):
    """Replace atomically; never follow an existing symlink or retain a hard link."""
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def reserve_ports():
    """Find three distinct loopback ports while holding all three reservations."""
    ports, reservations = {}, []
    try:
        for name, preferred in PREFERRED_PORTS.items():
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            reservations.append(listener)
            try:
                listener.bind(('127.0.0.1', preferred))
            except OSError:
                listener.bind(('127.0.0.1', 0))
            ports[name] = listener.getsockname()[1]
        return ports
    finally:
        for listener in reservations:
            listener.close()


def valid_ports(ports):
    return (isinstance(ports, dict) and set(ports) == set(MAIN_PORTS)
            and all(type(port) is int and 1024 <= port <= 65535 for port in ports.values())
            and len(set(ports.values())) == 3)


def read_state(path, identity):
    if not path.exists() and not path.is_symlink():
        return None
    private_file(path)
    try:
        state = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise ValueError('The saved connection state is invalid; it was left unchanged.') from None
    if (not isinstance(state, dict) or state.get('schema') != 1 or state.get('identity') != identity
            or not valid_ports(state.get('local_ports'))):
        raise ValueError('This node ID already belongs to a different or invalid main connection; use a new node ID.')
    return state


def ensure_link_key(directory):
    key = directory / 'id_ed25519'
    if not key.exists() and not key.is_symlink():
        subprocess.run(['/usr/bin/ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'fireisp-node-link',
                        '-f', str(key)], check=True, stdout=subprocess.DEVNULL)
    private_file(key, maximum=16384)
    result = subprocess.run(['/usr/bin/ssh-keygen', '-y', '-P', '', '-f', str(key)],
                            check=True, stdout=subprocess.PIPE, text=True)
    # Newer OpenSSH preserves the private key's comment in -y output. The
    # enrollment protocol needs only the algorithm and public key blob.
    public_key = ' '.join(result.stdout.split()[:2])
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]{40,200}={0,3}', public_key):
        raise ValueError('The saved connection key must be an unencrypted Ed25519 key.')
    return key, public_key


def ssh_options(known_hosts, ssh_port, *, interactive):
    return [
        '-F', '/dev/null', '-T', '-p', str(ssh_port), '-o', f'UserKnownHostsFile={known_hosts}',
        '-o', 'GlobalKnownHostsFile=/dev/null', '-o', f'StrictHostKeyChecking={"ask" if interactive else "yes"}',
        '-o', 'UpdateHostKeys=no', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes',
        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
    ] + ([] if interactive else ['-o', 'BatchMode=yes'])


def request_enrollment(main_host, ssh_port, admin_user, admin_key, known_hosts, payload, *, interactive=True):
    command = ['/usr/bin/ssh', *ssh_options(known_hosts, ssh_port, interactive=interactive)]
    if admin_key:
        # sudo installers may use their invoking operator's private key. This
        # exception applies only to that explicitly selected authentication key;
        # generated link keys and connection state remain installer-owned.
        owners = {os.geteuid()}
        sudo_uid = os.environ.get('SUDO_UID', '')
        if (os.getuid() == 0 and os.geteuid() == 0 and re.fullmatch(r'[1-9][0-9]{0,9}', sudo_uid)
                and int(sudo_uid) < 2 ** 32 - 1):
            owners.add(int(sudo_uid))
        admin_key = Path(admin_key).expanduser().absolute()
        private_file(admin_key, maximum=65536, owners=owners)
        command += ['-i', str(admin_key), '-o', 'IdentitiesOnly=yes']
    remote = ['python3', '/opt/fireisp/app/deploy/pairing.py', 'prepare']
    if admin_user != 'root':
        remote = ['sudo', '-n', *remote]
    command += [f'{admin_user}@{main_host}', shlex.join(remote)]
    # OpenSSH asks for passwords/host-key confirmation through /dev/tty, even
    # though stdin carries JSON. Only stdout is captured; no secret is logged.
    result = subprocess.run(command, input=json.dumps(payload), stdout=subprocess.PIPE, text=True, check=True,
                            timeout=None if interactive else 240)
    if len(result.stdout) > 131072:
        raise ValueError('The main server returned an oversized enrollment response.')
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise ValueError('The main server did not return a valid enrollment response. Upgrade its installer first.') from None


def validate_manifest(manifest, node_id, roles, network_endpoint=''):
    """Treat authenticated remote data as configuration, never as executable text."""
    if (not isinstance(manifest, dict) or manifest.get('schema') != 1 or manifest.get('node_id') != node_id
            or manifest.get('ssh_user') != 'fireisp-link'
            or not isinstance(manifest.get('release'), str)
            or not re.fullmatch(r'[0-9a-f]{40}', manifest['release'])
            or manifest.get('ports') != MAIN_PORTS):
        raise ValueError('The main server returned an incompatible enrollment identity or release.')
    values = manifest.get('environment')
    allowed = COMMON_ENVIRONMENT | (NETWORK_ENVIRONMENT if 'network' in roles else frozenset())
    if (not isinstance(values, dict) or not set(values).issubset(allowed)
            or any(not isinstance(value, str) or len(value) > 8192 or any(c in value for c in '\r\n\0')
                   for value in values.values())):
        raise ValueError('The main server returned invalid or unsupported environment settings.')
    for key in ('SECRET_KEY', 'ENCRYPTION_KEY', 'DATABASE_URL', 'REDIS_URL', 'ALLOWED_HOSTS'):
        if not values.get(key):
            raise ValueError('The main server omitted a required application setting.')
    try:
        encryption_key = base64.urlsafe_b64decode(values['ENCRYPTION_KEY'].encode('ascii'))
    except (ValueError, UnicodeError):
        encryption_key = b''
    if len(values['SECRET_KEY']) < 32 or len(encryption_key) != 32 or '*' in values['ALLOWED_HOSTS']:
        raise ValueError('The main server returned invalid application keys or allowed hosts.')
    for name, kind in (('DATABASE_URL', 'database'), ('REDIS_URL', 'redis')):
        try:
            url = urlsplit(values[name])
            schemes = ('postgres', 'postgresql') if kind == 'database' else ('redis',)
            if (url.scheme not in schemes or url.hostname != '127.0.0.1' or url.port != MAIN_PORTS[kind]
                    or url.fragment):
                raise ValueError()
            forbidden = {'host', 'hostaddr', 'service', 'user', 'username', 'password', 'dbname', 'db', 'port'}
            if forbidden & set(parse_qs(url.query)):
                raise ValueError()
            if kind == 'database' and (not url.username or url.username in {'root', 'postgres'}
                                       or not url.password or not url.path.strip('/')):
                raise ValueError()
            if kind == 'redis' and not re.fullmatch(r'/[0-9]+', url.path):
                raise ValueError()
        except ValueError:
            raise ValueError('The main server returned an invalid loopback service URL.') from None
    if 'network' in roles:
        if (not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', values.get('NETWORK_RADIUS_TOKEN', ''))
                or values.get('NETWORK_PUBLIC_ENDPOINT') != network_endpoint
                or values.get('NETWORK_RADIUS_URL') != 'http://127.0.0.1:18000/network/radius'):
            raise ValueError('The main server returned an invalid network node identity.')
    return dict(values)


def forwarded_environment(values, ports, roles):
    result = dict(values)
    for name, kind in (('DATABASE_URL', 'database'), ('REDIS_URL', 'redis')):
        url = urlsplit(result[name])
        # Preserve encoded credentials verbatim: decoding/re-encoding could
        # alter a password containing delimiters or literal percent signs.
        credentials = url.netloc.rpartition('@')[0]
        authority = (credentials + '@' if '@' in url.netloc else '') + f'127.0.0.1:{ports[kind]}'
        result[name] = urlunsplit((url.scheme, authority, url.path, url.query, ''))
    if 'network' in roles:
        result['NETWORK_RADIUS_URL'] = f'http://127.0.0.1:{ports["web"]}/network/radius'
    return result


def systemd_quote(value):
    # ExecStart is parsed by systemd, not a shell. Disable its two expansion
    # mechanisms as well as quoting whitespace/backslashes.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def tunnel_unit(main_host, ssh_port, node_id, directory, ports):
    command = ['/usr/bin/ssh', '-N', *ssh_options(directory / 'known_hosts', ssh_port, interactive=False)]
    # ClearAllForwardings=yes is desirable for enrollment, but would also clear
    # the explicitly configured tunnel. Set it to no for this dedicated process.
    command[command.index('ClearAllForwardings=yes')] = 'ClearAllForwardings=no'
    command += ['-i', str(directory / 'id_ed25519'), '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-o', 'ExitOnForwardFailure=yes', '-o', 'ControlMaster=no', '-o', 'ControlPath=none']
    for kind, remote_port in MAIN_PORTS.items():
        command += ['-L', f'127.0.0.1:{ports[kind]}:127.0.0.1:{remote_port}']
    command += [f'fireisp-link@{main_host}']
    return '\n'.join([
        '[Unit]', f'Description=FireISP private main connection ({node_id})',
        'Wants=network-online.target', 'After=network-online.target', 'StartLimitIntervalSec=0', '',
        '[Service]', 'Type=simple', 'User=root', 'UMask=0077',
        'ExecStart=' + ' '.join(systemd_quote(argument) for argument in command),
        'Restart=always', 'RestartSec=5', 'TimeoutStopSec=10',
        'NoNewPrivileges=yes', 'PrivateTmp=yes', 'ProtectSystem=strict', 'ProtectHome=yes',
        'ProtectKernelTunables=yes', 'ProtectKernelModules=yes', 'ProtectControlGroups=yes',
        'RestrictSUIDSGID=yes', 'LockPersonality=yes', 'CapabilityBoundingSet=',
        'RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX', 'StandardOutput=null', '',
        '[Install]', 'WantedBy=multi-user.target', '',
    ])


def wait_for_tunnel(ports, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for port in ports.values():
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    pass
            return
        except OSError:
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError('The private connection did not start. Check SSH access and the fireisp-link service, then rerun installation.')


def require_root():
    if os.geteuid() != 0:
        raise ValueError('Run the installation with sudo so the connection can survive reboots.')


def connect_main(main_host, ssh_port, admin_user, admin_key, node_id, roles, network_endpoint='', *,
                 directory=Path('/etc/fireisp/connections'), systemd_directory=Path('/etc/systemd/system'), timeout=30,
                 interactive=True):
    """Enroll once, or refresh modules using the existing key and pinned identity.

    Returns only file paths and nonsecret metadata. A role installer reads the
    protected ``environment_file`` and checks the actual database/broker before
    launching application services.
    """
    require_root()
    validate_identity(main_host, ssh_port, admin_user, node_id, roles, network_endpoint)
    directory, systemd_directory = Path(directory), Path(systemd_directory)
    private_directory(directory)
    connection_directory = directory / node_id
    private_directory(connection_directory)
    lock_descriptor = os.open(connection_directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_descriptor, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another installer is already configuring this node connection.') from None
        identity = {'main_host': main_host, 'ssh_port': ssh_port, 'node_id': node_id}
        state_path = connection_directory / 'connection.json'
        state = read_state(state_path, identity)
        if state is None:
            state = {'schema': 1, 'identity': identity, 'local_ports': reserve_ports()}
            write_private(state_path, json.dumps(state, indent=2) + '\n')
        ports = state['local_ports']
        try:
            _, public_key = ensure_link_key(connection_directory)
            known_hosts = connection_directory / 'known_hosts'
            if not known_hosts.exists() and not known_hosts.is_symlink():
                write_private(known_hosts, '')
            private_file(known_hosts)
            manifest = request_enrollment(main_host, ssh_port, admin_user, admin_key, known_hosts, {
                'node_id': node_id, 'roles': list(roles), 'public_key': public_key,
                'network_endpoint': network_endpoint if 'network' in roles else '',
            }, interactive=interactive)
            private_file(known_hosts)
            if not known_hosts.read_text().strip():
                raise ValueError('OpenSSH did not save the main server host key; enrollment cannot continue.')
            values = validate_manifest(manifest, node_id, roles, network_endpoint)
            environment = forwarded_environment(values, ports, roles)
            environment_file = connection_directory / 'node.env'
            write_private(environment_file, ''.join(f'{name}={shlex.quote(value)}\n'
                                                   for name, value in sorted(environment.items())))
            service = f'fireisp-link-{node_id}.service'
            unit_path = systemd_directory / service
            unit = tunnel_unit(main_host, ssh_port, node_id, connection_directory, ports)
            changed = not unit_path.exists() or unit_path.read_text() != unit
            if changed:
                write_private(unit_path, unit)
                subprocess.run(['systemctl', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', 'enable', '--now', service], check=True)
            if changed:
                subprocess.run(['systemctl', 'restart', service], check=True)
            wait_for_tunnel(ports, timeout)
            state.update({'release': manifest['release'], 'roles': sorted(roles), 'service': service})
            write_private(state_path, json.dumps(state, indent=2) + '\n')
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            raise RuntimeError('Connection setup failed. Verify SSH authentication, sudo access on the main server, and its current installer.') from None
    return {'release': manifest['release'], 'node_id': node_id, 'roles': list(roles),
            'environment_file': environment_file, 'directory': connection_directory, 'service': service, 'ports': ports}
