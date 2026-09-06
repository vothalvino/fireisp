#!/usr/bin/env python3
"""Install one FireISP execution role against existing private PostgreSQL and Redis."""
import argparse
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit, urlunsplit


ROLES = ('web', 'worker', 'billing', 'fiscal', 'scheduler', 'network')
COMMON_ENVIRONMENT = {
    'SECRET_KEY', 'ENCRYPTION_KEY', 'DATABASE_URL', 'REDIS_URL', 'ALLOWED_HOSTS',
    'CSRF_TRUSTED_ORIGINS', 'FIREISP_VERSION',
}
NETWORK_ENVIRONMENT = {'NETWORK_RADIUS_TOKEN', 'NETWORK_PUBLIC_ENDPOINT'}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                         ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', 'fc00::/7'))
REVISION_LABEL = 'org.opencontainers.image.revision'
PREFLIGHT = '''import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fireisp.settings')
try:
 import django
 django.setup()
 from django.db import connection
 from django.db.migrations.executor import MigrationExecutor
 import redis
 if connection.vendor != 'postgresql':
  raise RuntimeError('PostgreSQL is required')
 connection.settings_dict.setdefault('OPTIONS', {})['connect_timeout'] = 5
 with connection.cursor() as cursor:
  cursor.execute('SELECT rolsuper OR rolcreaterole OR rolcreatedb FROM pg_roles WHERE rolname = current_user')
  row = cursor.fetchone()
  if row is None or row[0]:
   raise RuntimeError('Application database role must not have administrative permissions')
 executor = MigrationExecutor(connection)
 if executor.migration_plan(executor.loader.graph.leaf_nodes()):
  raise RuntimeError('Database migrations are pending')
 from core.models import DeploymentState
 expected_release = DeploymentState.objects.filter(pk=1).values_list('release', flat=True).first()
 if not expected_release or expected_release != os.environ['FIREISP_RELEASE']:
  raise RuntimeError('The node release does not match the initialized cluster release')
 redis.Redis.from_url(os.environ['REDIS_URL'], socket_connect_timeout=5, socket_timeout=5).ping()
except Exception:
 print('Node preflight failed: verify private connections, application DB permissions, shared keys, cluster release, and applied migrations.', file=sys.stderr)
 sys.exit(1)
print('Node preflight passed: PostgreSQL application role, migrations, cluster release, and broker.')
'''


def load_environment(path):
    """Read an operator-owned file without expanding variables or running shell text."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid not in (0, os.geteuid()):
        raise ValueError('The environment file must be a regular owner-only file (chmod 600).')
    if metadata.st_size > 65536:
        raise ValueError('The environment file exceeds the size limit.')
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or not re.fullmatch(r'[A-Z][A-Z0-9_]*', key) or key in values:
            raise ValueError('The environment file contains an invalid or duplicate key.')
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError:
            raise ValueError('The environment file contains invalid quoting.') from None
        if len(parts) > 1 or any(character in value for character in '\r\n\0'):
            raise ValueError('Environment values must occupy one line; quote values containing spaces.')
        values[key] = parts[0] if parts else ''
    return values


def private_host(host, port, allow_loopback=False):
    if not host or '%' in host:
        raise ValueError('A private service hostname or IP address is required.')
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = {ipaddress.ip_address(answer[4][0]) for answer in answers}
    except (ValueError, OSError):
        raise ValueError('A private service hostname could not be resolved.') from None
    if not addresses:
        raise ValueError('A private service hostname could not be resolved.')
    for address in addresses:
        if address.is_loopback and allow_loopback:
            continue
        if not any(address in subnet for subnet in PRIVATE_NETWORKS):
            raise ValueError('Service endpoints must resolve only to private IPs; loopback tunnels require --allow-loopback-tunnels.')


def validate_url(value, kind, allow_loopback=False):
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ValueError(f'{kind} has an invalid URL.') from None
    schemes = {'DATABASE_URL': ('postgres', 'postgresql'), 'REDIS_URL': ('redis', 'rediss'),
               'NETWORK_RADIUS_URL': ('http', 'https')}[kind]
    if url.scheme not in schemes or url.fragment or not url.hostname:
        raise ValueError(f'{kind} has an invalid scheme, hostname, or fragment.')
    if kind == 'DATABASE_URL':
        if not url.username or url.username.lower() in {'postgres', 'root'} or not url.password or not url.path.strip('/'):
            raise ValueError('DATABASE_URL requires a named application database, a non-superuser account, and a password.')
        query = parse_qs(url.query)
        if any(key in query for key in ('host', 'hostaddr', 'service', 'user', 'password', 'dbname')):
            raise ValueError('DATABASE_URL cannot override its validated connection parameters in the query.')
    elif kind == 'REDIS_URL':
        if url.path and not re.fullmatch(r'/[0-9]+', url.path):
            raise ValueError('REDIS_URL must use a numeric database number.')
        query = parse_qs(url.query)
        if any(key in query for key in ('host', 'port', 'username', 'password', 'db')):
            raise ValueError('REDIS_URL cannot override validated connection parameters in the query.')
        if url.scheme == 'rediss' and query.get('ssl_cert_reqs', ['required']) != ['required']:
            raise ValueError('REDIS_URL TLS certificate verification must be required.')
        if url.scheme == 'rediss' and query.get('ssl_check_hostname', ['true']) not in (['true'], ['True'], ['1']):
            raise ValueError('REDIS_URL TLS hostname verification must be enabled.')
    elif url.username or url.password or url.query or url.path.rstrip('/') != '/network/radius':
        raise ValueError('NETWORK_RADIUS_URL must identify the private /network/radius endpoint without URL credentials.')
    private_host(url.hostname, port or {'DATABASE_URL': 5432, 'REDIS_URL': 6379,
                                     'NETWORK_RADIUS_URL': 443 if url.scheme == 'https' else 80}[kind], allow_loopback)


def runtime_environment(values, role, node_id, release, allow_loopback=False):
    if role not in ROLES or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,47}', node_id):
        raise ValueError('Use a supported role and a node ID containing at most 48 lowercase letters, digits, or hyphens.')
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('Release must be the full 40-character Git commit SHA used by the control server.')
    for key in ('SECRET_KEY', 'ENCRYPTION_KEY', 'DATABASE_URL', 'REDIS_URL', 'ALLOWED_HOSTS'):
        if not values.get(key):
            raise ValueError(f'{key} is required in the protected environment file.')
    if len(values['SECRET_KEY']) < 32 or values['SECRET_KEY'].startswith('development-'):
        raise ValueError('Use the existing cluster SECRET_KEY with at least 32 characters.')
    try:
        if len(base64.urlsafe_b64decode(values['ENCRYPTION_KEY'].encode())) != 32:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise ValueError('Use the existing cluster Fernet ENCRYPTION_KEY.') from None
    if '*' in values['ALLOWED_HOSTS']:
        raise ValueError('ALLOWED_HOSTS must list explicit hostnames.')
    for key in ('DATABASE_URL', 'REDIS_URL'):
        validate_url(values[key], key, allow_loopback)
    allowed = COMMON_ENVIRONMENT | (NETWORK_ENVIRONMENT if role == 'network' else set())
    environment = {key: value for key, value in values.items() if key in allowed}
    environment.update({'DEBUG': 'false', 'FIREISP_NODE_ID': node_id, 'FIREISP_NODE_ROLE': role,
                        'FIREISP_RELEASE': release})
    if role == 'network':
        if len(node_id) > 40:
            raise ValueError('Network node IDs must fit the registered 40-character identity.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', values.get('NETWORK_RADIUS_TOKEN', '')):
            raise ValueError('The network role requires its registered NETWORK_RADIUS_TOKEN.')
        validate_url(values.get('NETWORK_RADIUS_URL', ''), 'NETWORK_RADIUS_URL', allow_loopback)
        try:
            address = ipaddress.ip_address(values.get('NETWORK_PUBLIC_ENDPOINT', ''))
            if not address.is_global or address.version != 4:
                raise ValueError()
        except ValueError:
            raise ValueError('The network role requires its own public IPv4 NETWORK_PUBLIC_ENDPOINT for router tunnels.') from None
        callback = urlsplit(values['NETWORK_RADIUS_URL'])
        environment.update({'NETWORK_NODE_ID': node_id, 'NETWORK_AGENT_SOCKET': '/run/fireisp-network/agent.sock',
                            'NETWORK_HEALTH_URL': urlunsplit((callback.scheme, callback.netloc, '/healthz', '', ''))})
    return environment


def build_compose(environment, role, node_id, image, *, concurrency=1, web_port=18000,
                  radius_url='', agent_image='', radius_image='', ca_directory=None):
    if not 1 <= concurrency <= 32 or not 1024 <= web_port <= 65535:
        raise ValueError('Concurrency must be 1–32 and the loopback web port must be 1024–65535.')
    # Compose interpolates dollar signs even inside JSON strings. Double them so the
    # original secret is delivered literally to the container, never by a shell.
    escape = lambda values: {key: value.replace('$', '$$') for key, value in values.items()}
    service = {'image': image, 'restart': 'unless-stopped', 'network_mode': 'host',
               'environment': escape(environment), 'read_only': True,
               'tmpfs': ['/tmp:size=64m', '/home/fireisp/.cache:size=64m'],
               'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
               'logging': {'driver': 'local', 'options': {'max-size': '10m', 'max-file': '3'}},
               'stop_grace_period': '180s'}
    if ca_directory:
        service['volumes'] = [{'type': 'bind', 'source': str(ca_directory),
                               'target': '/run/fireisp-certs', 'read_only': True}]
    if role == 'web':
        service['command'] = ['gunicorn', 'fireisp.wsgi:application', '--bind', f'127.0.0.1:{web_port}',
                              '--workers', str(concurrency), '--threads', '2', '--timeout', '100',
                              '--max-requests', '1000', '--max-requests-jitter', '100']
        service['healthcheck'] = {'test': ['CMD', 'python', '-c',
            "import os,urllib.request; urllib.request.urlopen(urllib.request.Request(" +
            repr(f'http://127.0.0.1:{web_port}/healthz') +
            ",headers={'Host':os.environ['ALLOWED_HOSTS'].split(',')[0],'X-Forwarded-Proto':'https'}),timeout=5)"],
            'interval': '15s', 'timeout': '7s', 'retries': 5, 'start_period': '30s'}
    elif role == 'scheduler':
        service['command'] = ['python', 'manage.py', 'run_scheduler']
    elif role == 'network':
        service['command'] = ['python', 'manage.py', 'run_network_jobs']
        service.setdefault('volumes', []).append('network_socket:/run/fireisp-network')
    else:
        service['command'] = ['python', 'manage.py', 'run_role', '--role', role, '--concurrency', str(concurrency)]
    result = {'name': f'fireisp-node-{node_id}', 'services': {role: service}}
    if role == 'network':
        result['volumes'] = {name: {} for name in ('network_socket', 'network_state', 'radius_config', 'radius_accounting')}
        result['services']['network-agent'] = {
            'image': agent_image, 'restart': 'unless-stopped', 'network_mode': 'host',
            'environment': {'NETWORK_WORKER_UIDS': '1000', 'NETWORK_WORKER_GID': '1000', 'NETWORK_NODE_ID': node_id},
            'cap_drop': ['ALL'], 'cap_add': ['NET_ADMIN', 'NET_RAW', 'CHOWN', 'DAC_OVERRIDE', 'SETUID', 'SETGID'],
            'devices': ['/dev/net/tun:/dev/net/tun', '/dev/ppp:/dev/ppp'],
            'security_opt': ['no-new-privileges:true'],
            'volumes': ['network_socket:/run/fireisp-network', 'network_state:/var/lib/fireisp-network',
                        'radius_config:/var/lib/fireisp-radius']}
        radius = {'image': radius_image, 'restart': 'unless-stopped', 'network_mode': 'host',
                  'environment': escape({'NETWORK_RADIUS_TOKEN': environment['NETWORK_RADIUS_TOKEN'],
                                         'NETWORK_RADIUS_URL': radius_url, 'NETWORK_NODE_ID': node_id}),
                  'volumes': ['radius_config:/var/lib/fireisp-radius:ro', 'radius_accounting:/var/log/freeradius']}
        if ca_directory:
            radius['volumes'].append({'type': 'bind', 'source': str(ca_directory),
                                      'target': '/run/fireisp-certs', 'read_only': True})
        result['services']['radius'] = radius
    return result


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(value)


def run(command, **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def build_release_image(source, release, image, dockerfile):
    # Build only tracked files at the declared release, excluding working-tree
    # changes, untracked credentials, and local environment files by construction.
    actual = subprocess.check_output(['git', '-C', str(source), 'rev-parse', f'{release}^{{commit}}'], text=True).strip()
    if actual != release:
        raise ValueError('The source repository does not contain the requested release.')
    with tempfile.TemporaryFile() as archive:
        run(['git', '-C', str(source), 'archive', '--format=tar', release], stdout=archive)
        archive.seek(0)
        run(['docker', 'build', '--pull', '--build-arg', f'FIREISP_RELEASE={release}', '--label', f'{REVISION_LABEL}={release}',
             '--tag', image, '--file', dockerfile, '-'], stdin=archive)


def verify_image(image, release):
    label = subprocess.check_output(['docker', 'image', 'inspect', '--format',
                                     '{{index .Config.Labels "' + REVISION_LABEL + '"}}', image], text=True).strip()
    if label != release:
        raise ValueError('An image revision label does not match the requested release; no services were started.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--role', choices=ROLES, required=True)
    parser.add_argument('--node-id', required=True)
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--release', required=True, help='Full Git SHA deployed on the control server')
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--image', help='Existing application image carrying the matching OCI revision label')
    parser.add_argument('--agent-image', help='Matching existing network agent image for --role network')
    parser.add_argument('--radius-image', help='Matching existing RADIUS image for --role network')
    parser.add_argument('--pull', action='store_true', help='Pull supplied image names before validating revision labels')
    parser.add_argument('--allow-loopback-tunnels', action='store_true', help='Use pre-established, operator-managed local SSH tunnels')
    parser.add_argument('--check-only', action='store_true', help='Validate inputs and show only a redacted role summary; change nothing')
    parser.add_argument('--concurrency', type=int, default=1)
    parser.add_argument('--web-port', type=int, default=18000)
    parser.add_argument('--ca-directory', type=Path, help='Read-only certificates directory mounted at /run/fireisp-certs')
    parser.add_argument('--directory', type=Path, help='Default: /opt/fireisp/nodes/NODE-ID')
    options = parser.parse_args()
    try:
        values = load_environment(options.env_file)
        environment = runtime_environment(values, options.role, options.node_id, options.release, options.allow_loopback_tunnels)
        if options.ca_directory:
            options.ca_directory = options.ca_directory.resolve(strict=True)
            if not options.ca_directory.is_dir():
                raise ValueError('The certificate path must be a directory.')
        images = {'app': options.image or f'fireisp:{options.release}',
                  'agent': options.agent_image or f'fireisp-network-agent:{options.release}',
                  'radius': options.radius_image or f'fireisp-radius:{options.release}'}
        if options.role == 'network' and options.image and not (options.agent_image and options.radius_image):
            raise ValueError('An existing network deployment needs --image, --agent-image, and --radius-image.')
        if any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._/@:+-]*', image) for image in images.values()):
            raise ValueError('Image references contain unsupported characters.')
        compose = build_compose(environment, options.role, options.node_id, images['app'],
                                concurrency=options.concurrency, web_port=options.web_port,
                                radius_url=values.get('NETWORK_RADIUS_URL', ''), agent_image=images['agent'],
                                radius_image=images['radius'], ca_directory=options.ca_directory)
        if options.check_only:
            print(json.dumps({'valid': True, 'role': options.role, 'node_id': options.node_id,
                              'release': options.release, 'services': sorted(compose['services']),
                              'database_or_broker_created': False, 'credentials_printed': False,
                              'connectivity_and_image_verification': 'performed during installation'}))
            return
        if os.geteuid() != 0:
            raise ValueError('Run the installation as root; --check-only does not require root.')
        from deploy.install import supported_ubuntu
        if not supported_ubuntu(Path('/etc/os-release').read_text()):
            raise ValueError('Node installation supports Ubuntu 24.04 only.')
        if not shutil.which('docker'):
            raise ValueError('Install Docker Engine with its Compose plugin before installing a role.')
        run(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
        directory = options.directory or Path('/opt/fireisp/nodes') / options.node_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise ValueError('The deployment directory must not be a symlink.')
        directory.chmod(0o700)
        import fcntl
        with (directory / '.install.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for kind in (('app', 'agent', 'radius') if options.role == 'network' else ('app',)):
                supplied = {'app': options.image, 'agent': options.agent_image, 'radius': options.radius_image}[kind]
                if not supplied:
                    dockerfile = {'app': 'Dockerfile', 'agent': 'deploy/network/Dockerfile.agent',
                                  'radius': 'deploy/network/Dockerfile.radius'}[kind]
                    build_release_image(options.source, options.release, images[kind], dockerfile)
                elif options.pull:
                    run(['docker', 'pull', images[kind]])
                verify_image(images[kind], options.release)
            candidate = directory / 'compose.next.json'
            write_private(candidate, json.dumps(compose, indent=2) + '\n')
            command = ['docker', 'compose', '--project-directory', str(directory), '-f', str(candidate)]
            # Never print resolved Compose configuration: it contains runtime secrets.
            run(command + ['config', '--quiet'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            run(command + ['run', '--rm', '--no-deps', options.role, 'python', '-c', PREFLIGHT])
            if options.role == 'network':
                run(['modprobe', 'ppp_generic'])
                if not Path('/dev/ppp').exists():
                    run(['mknod', '-m', '600', '/dev/ppp', 'c', '108', '0'])
                if not Path('/dev/net/tun').exists():
                    raise ValueError('The network role requires the Linux TUN device.')
            installed = directory / 'compose.json'
            if installed.exists():
                write_private(directory / 'compose.previous.json', installed.read_text())
            candidate.replace(installed)
            command = ['docker', 'compose', '--project-directory', str(directory), '-f', str(installed)]
            run(command + ['up', '-d', '--remove-orphans', '--wait', '--wait-timeout', '90'])
            write_private(directory / 'release.json', json.dumps({'release': options.release, 'role': options.role,
                                                                 'node_id': options.node_id, 'images': images}) + '\n')
            print(f'Role {options.role} started as {options.node_id}. Verify its runtime heartbeat and a routed task before draining the old role. Private configuration: {directory}/compose.json')
    except (ValueError, OSError, subprocess.SubprocessError):
        # Subprocess/database errors may include a DSN. Validation text above is
        # designed to be safe, but callers never receive raw third-party errors.
        error = sys.exception()
        if isinstance(error, ValueError):
            parser.error(str(error))
        parser.error('Node installation failed. Check private prerequisites and protected configuration; credentials were not printed.')


if __name__ == '__main__':
    # Allows direct execution from an inspected checkout without pip installation.
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    main()
