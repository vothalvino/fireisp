#!/usr/bin/env python3
"""Enroll a worker through an operator-authenticated SSH session on the main server.

The request and response travel on stdin/stdout. Never log the response: it
contains the application's shared database credentials and encryption keys.
"""
import argparse
import base64
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import stat
import struct
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit


ROLES = ('worker', 'billing', 'fiscal', 'network', 'scheduler', 'web')
SSH_USER = 'fireisp-link'
SSH_HOME = Path('/var/lib/fireisp-link')
STATE_DIRECTORY = Path('/etc/fireisp/pairings')
SSHD_CONFIG = Path('/etc/ssh/sshd_config.d/90-fireisp-link.conf')
PORTS = {'database': 15432, 'redis': 16379, 'web': 18000}
ENVIRONMENT_KEYS = {
    'SECRET_KEY', 'ENCRYPTION_KEY', 'DATABASE_URL', 'REDIS_URL', 'ALLOWED_HOSTS',
    'CSRF_TRUSTED_ORIGINS', 'FIREISP_VERSION',
}
SSHD_RULES = '''# Managed by FireISP. This account can only open local TCP forwards.
Match User fireisp-link
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding local
    AllowStreamLocalForwarding no
    PermitOpen 127.0.0.1:15432 127.0.0.1:16379 127.0.0.1:18000
    PermitListen none
    PermitTTY no
    PermitTunnel no
    X11Forwarding no
    AllowAgentForwarding no
    ForceCommand /usr/sbin/nologin
'''
REGISTER_NODE = '''import hashlib, json, os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fireisp.settings')
import django
django.setup()
from core.models import DeploymentState
from django.core.management import call_command
from network.models import NetworkNode
request = json.load(sys.stdin)
if DeploymentState.objects.filter(pk=1).values_list('release', flat=True).first() != request['release']:
 raise RuntimeError('The running release has not been initialized in the database.')
if request.get('network_token'):
 node_id = request['node_id'] + '-network'
 node = NetworkNode.objects.filter(pk=node_id).first()
 if node:
  if node.public_endpoint != request['network_endpoint'] or node.radius_token_digest != hashlib.sha256(request['network_token'].encode()).hexdigest():
   raise RuntimeError('The network identity already exists with different settings.')
 else:
  import io
  sys.stdin = io.StringIO(request['network_token'])
  call_command('register_network_node', node_id, endpoint=request['network_endpoint'], radius_token_stdin=True, stdout=io.StringIO())
'''


def run(command, **kwargs):
    """Capture output, including errors, so child processes cannot leak secrets."""
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=60, **kwargs)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('A required main-server command failed; inspect the service locally.') from None


def validate_request(request):
    if not isinstance(request, dict) or set(request) - {'node_id', 'roles', 'public_key', 'network_endpoint'}:
        raise ValueError('Unsupported pairing request fields.')
    node_id = request.get('node_id', '')
    if not isinstance(node_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,29}', node_id):
        raise ValueError('Node ID must contain 1–30 lowercase letters, digits, or hyphens.')
    roles = request.get('roles')
    if not isinstance(roles, list) or not roles or any(not isinstance(role, str) or role not in ROLES for role in roles):
        raise ValueError('Select at least one supported worker role.')
    if len(roles) != len(set(roles)):
        raise ValueError('Worker roles must not be duplicated.')
    public_key = request.get('public_key', '')
    if not isinstance(public_key, str) or len(public_key) > 1024 or any(c in public_key for c in '\r\n\0'):
        raise ValueError('A single ed25519 SSH public key is required.')
    parts = public_key.split()
    if len(parts) not in (2, 3) or parts[0] != 'ssh-ed25519':
        raise ValueError('A single ed25519 SSH public key is required.')
    try:
        raw = base64.b64decode(parts[1], validate=True)
        prefix = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32)
        if len(raw) != len(prefix) + 32 or not raw.startswith(prefix):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('The ed25519 public key is invalid.') from None
    endpoint = request.get('network_endpoint', '')
    if not isinstance(endpoint, str):
        raise ValueError('The network endpoint must be an IPv4 address.')
    if 'network' in roles:
        try:
            address = ipaddress.ip_address(endpoint)
            if address.version != 4 or not address.is_global:
                raise ValueError()
        except ValueError:
            raise ValueError('Network workers require their own public IPv4 address.') from None
    elif endpoint:
        raise ValueError('Only network workers use a network endpoint.')
    return {'node_id': node_id, 'roles': sorted(roles), 'public_key': ' '.join(parts[:2]),
            'network_endpoint': endpoint}


def forwarding_key(request):
    destinations = ','.join(f'permitopen="127.0.0.1:{port}"' for port in PORTS.values())
    return f'restrict,port-forwarding,{destinations},command="/usr/sbin/nologin" {request["public_key"]} fireisp:{request["node_id"]}'


def paired_environment(values):
    """Return application settings only, with database services behind SSH."""
    if not isinstance(values, dict):
        raise ValueError('The main server has invalid application configuration.')
    release = values.get('FIREISP_RELEASE', '')
    if not isinstance(release, str) or not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('Upgrade the main server to a versioned FireISP release before pairing.')
    environment = {key: value for key, value in values.items() if key in ENVIRONMENT_KEYS}
    if any(not isinstance(value, str) or any(c in value for c in '\r\n\0') for value in environment.values()):
        raise ValueError('The main server has invalid application configuration.')
    if len(environment.get('SECRET_KEY', '')) < 32 or not environment.get('ALLOWED_HOSTS') or '*' in environment['ALLOWED_HOSTS']:
        raise ValueError('The main server requires strong keys and explicit allowed hostnames.')
    try:
        if len(base64.urlsafe_b64decode(environment.get('ENCRYPTION_KEY', '').encode())) != 32:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError('The main server has an invalid encryption key.') from None
    for name, host, source_port, destination_port in (
        ('DATABASE_URL', 'db', 5432, PORTS['database']),
        ('REDIS_URL', 'redis', 6379, PORTS['redis']),
    ):
        try:
            url = urlsplit(environment.get(name, ''))
            schemes = ('postgres', 'postgresql') if name == 'DATABASE_URL' else ('redis',)
            if url.scheme not in schemes or url.hostname != host or url.port not in (None, source_port) or url.fragment or url.query:
                raise ValueError()
            if name == 'DATABASE_URL' and (not url.password or not url.username or url.username.lower() in ('root', 'postgres') or not url.path.strip('/')):
                raise ValueError()
        except ValueError:
            raise ValueError(f'{name} must identify the main server\'s standard private application service.') from None
        credentials = url.netloc.rsplit('@', 1)[0] + '@' if '@' in url.netloc else ''
        environment[name] = urlunsplit((url.scheme, f'{credentials}127.0.0.1:{destination_port}', url.path, '', ''))
    return release, environment


def main_configuration(compose):
    try:
        config = json.loads(run(compose + ['config', '--format', 'json']).stdout)
        release, environment = paired_environment(config['services']['web']['environment'])
        for service, container_port, host_port in (('db', '5432/tcp', PORTS['database']),
                                                   ('redis', '6379/tcp', PORTS['redis']),
                                                   ('web', '8000/tcp', PORTS['web'])):
            container_id = run(compose + ['ps', '--quiet', service]).stdout.strip()
            if not re.fullmatch(r'[a-f0-9]{12,64}', container_id):
                raise ValueError('The main server must have one running database, broker, and web service.')
            container = json.loads(run(['docker', 'inspect', container_id]).stdout)[0]
            bindings = container['NetworkSettings']['Ports'].get(container_port)
            if bindings != [{'HostIp': '127.0.0.1', 'HostPort': str(host_port)}] or not container['State']['Running']:
                raise ValueError('Upgrade the main server: its database, broker, and web tunnel ports must be bound only to loopback.')
            if service == 'web' and container['Config']['Labels'].get('org.opencontainers.image.revision') != release:
                raise ValueError('The running web image does not match the configured main-server release.')
        return release, environment
    except (KeyError, TypeError, IndexError, json.JSONDecodeError):
        raise ValueError('The main server returned unexpected service configuration.') from None


def private_directory(path, uid=0, gid=0):
    try:
        path.mkdir(mode=0o700)
        os.chown(path, uid, gid)
    except FileExistsError:
        pass
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o077:
        raise ValueError('A pairing directory has unsafe ownership or permissions.')


def read_private(path, uid=0):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != uid or metadata.st_mode & 0o077 or metadata.st_size > 1048576:
            raise ValueError('A pairing file has unsafe ownership, permissions, or size.')
        return stream.read()


def write_private(path, value, uid=0, gid=0):
    read_private(path, uid=uid)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        os.fchown(fd, uid, gid)
        with os.fdopen(fd, 'w') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def profile_for(request, previous):
    if previous:
        if previous.get('node_id') != request['node_id'] or previous.get('public_key') != request['public_key']:
            raise ValueError('This node ID is already paired with a different SSH key; recover the original node key or use a new ID.')
        if 'network' in request['roles'] and previous.get('network_endpoint') and previous['network_endpoint'] != request['network_endpoint']:
            raise ValueError('An existing network endpoint cannot be changed through pairing; use a reviewed network migration.')
    profile = {**request, 'schema': 1}
    if previous and previous.get('network_token'):
        profile.update({'network_token': previous['network_token'], 'network_endpoint': previous['network_endpoint']})
    if 'network' in request['roles']:
        profile['network_token'] = previous.get('network_token') if previous else None
        profile['network_token'] = profile['network_token'] or secrets.token_urlsafe(48)
    return profile


def validate_sshd():
    run(['/usr/sbin/sshd', '-t'])
    required = {'authenticationmethods': 'publickey', 'passwordauthentication': 'no',
                'kbdinteractiveauthentication': 'no', 'allowtcpforwarding': 'local',
                'allowstreamlocalforwarding': 'no', 'permitlisten': 'none', 'permittty': 'no',
                'permittunnel': 'no', 'x11forwarding': 'no', 'allowagentforwarding': 'no',
                'forcecommand': '/usr/sbin/nologin',
                'permitopen': ' '.join(f'127.0.0.1:{port}' for port in PORTS.values())}
    addresses = {'127.0.0.1'}
    incoming = os.environ.get('SSH_CONNECTION', '').split()
    if incoming:
        try:
            addresses.add(str(ipaddress.ip_address(incoming[0])))
        except ValueError:
            raise ValueError('The operator SSH connection address is invalid.') from None
    for address in sorted(addresses):
        effective = run(['/usr/sbin/sshd', '-T', '-C', f'user={SSH_USER},host={address},addr={address}']).stdout
        settings = dict(line.split(' ', 1) for line in effective.splitlines() if ' ' in line)
        if any(settings.get(key) != value for key, value in required.items()):
            raise ValueError('Existing SSH settings conflict with the forwarding-only account restrictions.')


def configure_sshd(path=SSHD_CONFIG):
    previous = read_private(path)
    if previous == SSHD_RULES:
        validate_sshd()
        return
    write_private(path, SSHD_RULES)
    try:
        validate_sshd()
        run(['systemctl', 'reload', 'ssh'])
    except ValueError:
        if previous is None:
            path.unlink()
        else:
            write_private(path, previous)
        try:
            run(['systemctl', 'reload', 'ssh'])
        except ValueError:
            pass
        raise ValueError('The SSH restrictions could not be applied; the previous SSH configuration was restored.') from None


def authorize_link(request, home=SSH_HOME):
    try:
        account = pwd.getpwnam(SSH_USER)
    except KeyError:
        run(['useradd', '--system', '--home-dir', str(home), '--create-home', '--shell', '/usr/sbin/nologin', SSH_USER])
        # A non-password marker avoids a usable password. SSH itself requires
        # public-key authentication, independently of PAM account policy.
        run(['usermod', '--password', '*', SSH_USER])
        account = pwd.getpwnam(SSH_USER)
        os.chmod(home, 0o700)
    if account.pw_dir != str(home) or account.pw_shell != '/usr/sbin/nologin' or account.pw_uid == 0:
        raise ValueError('The dedicated forwarding account already exists with incompatible settings.')
    private_directory(home, account.pw_uid, account.pw_gid)
    private_directory(home / '.ssh', account.pw_uid, account.pw_gid)
    authorized = home / '.ssh' / 'authorized_keys'
    current = read_private(authorized, account.pw_uid) or ''
    entry = forwarding_key(request)
    tag = 'fireisp:' + request['node_id']
    tagged = [line for line in current.splitlines() if line.split() and line.split()[-1] == tag]
    if tagged and tagged != [entry]:
        raise ValueError('This node already has a different forwarding key; pairing cannot replace it silently.')
    configure_sshd()
    if not tagged:
        write_private(authorized, current.rstrip('\n') + ('\n' if current else '') + entry + '\n', account.pw_uid, account.pw_gid)


def prepare(request, project_directory=Path('/opt/fireisp/staging'), state_directory=STATE_DIRECTORY):
    request = validate_request(request)
    compose = ['docker', 'compose', '--project-directory', str(project_directory)]
    private_directory(state_directory.parent)
    private_directory(state_directory)
    lock_fd = os.open(state_directory / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'w') as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise ValueError('The pairing lock has unsafe ownership or permissions.')
        fcntl.flock(lock, fcntl.LOCK_EX)
        release, environment = main_configuration(compose)
        environment = environment.copy()
        profile_path = state_directory / (request['node_id'] + '.json')
        previous = read_private(profile_path)
        try:
            previous = json.loads(previous) if previous else None
            if previous is not None and not isinstance(previous, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('The saved pairing profile is invalid.') from None
        profile = profile_for(request, previous)
        # Save the generated network token first so an interrupted enrollment
        # reuses its identity when retried with the same node key.
        write_private(profile_path, json.dumps(profile, indent=2) + '\n')
        registration = {**profile, 'release': release}
        if 'network' not in request['roles']:
            registration.pop('network_token', None)
        run(compose + ['exec', '-T', 'web', 'python', '-c', REGISTER_NODE], input=json.dumps(registration))
        authorize_link(request)
        if 'network' in request['roles']:
            environment.update({'NETWORK_RADIUS_TOKEN': profile['network_token'],
                                'NETWORK_PUBLIC_ENDPOINT': profile['network_endpoint'],
                                'NETWORK_RADIUS_URL': f'http://127.0.0.1:{PORTS["web"]}/network/radius'})
        return {'schema': 1, 'release': release, 'node_id': request['node_id'], 'ssh_user': SSH_USER,
                'environment': environment, 'ports': PORTS.copy()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare',))
    options = parser.parse_args()
    if os.geteuid() != 0:
        parser.exit(1, 'Pairing must run as root on the main server; use operator SSH or sudo -n.\n')
    try:
        raw = sys.stdin.read(8193)
        if len(raw) > 8192:
            raise ValueError('The pairing request exceeds the size limit.')
        request = json.loads(raw)
        if options.action == 'prepare':
            response = prepare(request)
            print(json.dumps(response))
    except (ValueError, OSError):
        # Never include subprocess output or user-supplied secret values.
        parser.exit(1, 'Pairing failed. Check the main-server version, loopback service ports, node identity, and SSH configuration locally.\n')


if __name__ == '__main__':
    main()
