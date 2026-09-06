#!/usr/bin/env python3
"""Repeatable Ubuntu installer. Run from an inspected checkout as root."""
import argparse
import base64
from datetime import timedelta
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

def run(*args, **kwargs):
    return subprocess.run(list(args), check=True, **kwargs)

def write_private(path, contents):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream: stream.write(contents)

def supported_ubuntu(contents):
    fields = dict(line.split('=', 1) for line in contents.splitlines() if '=' in line and not line.startswith('#'))
    return fields.get('ID', '').strip('"\'') == 'ubuntu' and fields.get('VERSION_ID', '').strip('"\'') == '24.04'


def release_identity(source, explicit=None):
    if (source / '.git').exists():
        if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True,
                                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'}).strip():
            raise ValueError('Commit or discard source changes before installing a release.')
        release = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
        if explicit and explicit != release:
            raise ValueError('The requested release does not match the inspected source checkout.')
    elif explicit:
        release = explicit
    elif (source / 'RELEASE').is_file():
        release = (source / 'RELEASE').read_text().strip()
    else:
        raise ValueError('Use an inspected Git checkout or pass its full commit with --release.')
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('Release must be a full Git commit SHA.')
    return release


def profile_selection(value):
    roles = set(filter(None, (part.strip() for part in value.split(','))))
    if not roles <= {'billing', 'fiscal', 'network'}:
        raise ValueError('Local workers must be a comma-separated selection of billing,fiscal,network.')
    return ','.join(sorted(roles))


def existing_profiles(existing):
    matches = re.findall(r'^\s*(?:export\s+)?COMPOSE_PROFILES\s*=([^\r\n]*)$', existing, re.MULTILINE)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError('The environment contains duplicate COMPOSE_PROFILES entries; keep one explicit placement.')
    try:
        parts = shlex.split(matches[0], comments=True)
    except ValueError:
        raise ValueError('COMPOSE_PROFILES contains invalid quoting.') from None
    if len(parts) > 1:
        raise ValueError('Quote the complete COMPOSE_PROFILES selection on one line.')
    return profile_selection(parts[0] if parts else '')


def local_profiles(requested, existing):
    if requested is None and existing_profiles(existing) is not None:
        return None  # Preserve deliberate placement on later installer runs.
    return profile_selection('billing,fiscal,network' if requested is None else requested)


def drain_local_executors(compose, profiles):
    """See disabled/restarting roles too; stop executors before shared migration."""
    all_profiles = [*compose, '--profile', '*']
    # --all includes a restarting/exited container with a restart policy. Stopping
    # an already stopped service is harmless; omitting a restarting worker is not.
    present = set(subprocess.check_output(all_profiles + ['ps', '--all', '--services'], text=True).split())
    stopped = []
    for service in ('beat', 'worker', 'billing-worker', 'fiscal-worker', 'network-worker'):
        if service in present:
            run(*all_profiles, 'stop', '--timeout', '180', service)
            stopped.append(service)
    if 'network' not in profile_selection(profiles).split(','):
        # Keep local accounting/tunnel files intact for replay and reviewed cleanup.
        # Stop RADIUS only after its job executor has drained.
        for service in ('radius', 'network-agent'):
            if service in present:
                run(*all_profiles, 'stop', '--timeout', '30', service)
                stopped.append(service)
    return stopped


LOCAL_EXECUTOR_IDENTITIES = {
    'primary-events:worker', 'primary-billing:billing', 'primary-fiscal:fiscal',
    'primary-network:network', 'primary-scheduler:scheduler',
}


def active_remote_executors(nodes, now):
    """Reject known active remote roles; this does not perform a remote drain."""
    cutoff = now - timedelta(seconds=90)
    roles = {'worker', 'billing', 'fiscal', 'network', 'scheduler'}
    return sorted({node['identifier'] for node in nodes
                   if node['identifier'] not in LOCAL_EXECUTOR_IDENTITIES
                   and node['role'] in roles and node['status'] in {'ready', 'standby'}
                   and node['last_seen'] >= cutoff})


def require_remote_executors_drained(allow_release=None):
    # Invoked inside the newly built web image BEFORE migrations. The first
    # upgrade from an older release does not yet have the runtime registry table.
    from django.db import connection
    from django.utils import timezone
    from core.models import RuntimeNode
    if RuntimeNode._meta.db_table not in connection.introspection.table_names():
        return
    if allow_release:
        from core.models import DeploymentState
        from django.db.migrations.executor import MigrationExecutor
        if DeploymentState.objects.filter(pk=1).values_list('release', flat=True).first() == allow_release:
            executor = MigrationExecutor(connection)
            if not executor.migration_plan(executor.loader.graph.leaf_nodes()):
                # Placement-only reruns keep compatible remote workers available.
                return
    now = timezone.now()
    nodes = RuntimeNode.objects.filter(last_seen__gte=now - timedelta(seconds=90)).values(
        'identifier', 'role', 'status', 'last_seen')
    active = active_remote_executors(nodes, now)
    if active:
        raise SystemExit('Remote executors are still active: ' + ', '.join(active[:10]) +
                         '. Gracefully drain remote nodes before migrating; this installer only controls local roles.')


def update_public_environment(path, values):
    # Preserve generated credentials byte-for-byte while updating this inspected checkout and hostname.
    existing = path.read_text() if path.exists() else ''
    lines = []
    for line in existing.splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=', line)
        if not match or match[1] not in values:
            lines.append(line)
    for key, value in values.items():
        if any(character in str(value) for character in '\r\n\0'):
            raise ValueError('Environment values cannot contain line breaks.')
        escaped = str(value).replace('\\', '\\\\').replace("'", "\\'")
        lines.append(f"{key}='{escaped}'")
    write_private(path, '\n'.join(lines) + '\n')

def local_ipv6_addresses():
    try:
        interfaces = json.loads(subprocess.check_output(['ip', '-j', '-6', 'address', 'show', 'scope', 'global'], text=True, timeout=5))
        return {str(ipaddress.IPv6Address(address['local'])) for interface in interfaces for address in interface.get('addr_info', []) if address.get('family') == 'inet6'}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        return None

def dns_preflight(hostname, public_ip):
    expected = str(ipaddress.IPv4Address(public_ip))
    try:
        answers = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise RuntimeError(f'DNS lookup failed for {hostname}. Create an A record pointing to {expected}, wait for propagation, and rerun the installer.') from None
    ipv4 = {str(ipaddress.IPv4Address(answer[4][0])) for answer in answers if answer[0] == socket.AF_INET}
    ipv6 = {str(ipaddress.IPv6Address(answer[4][0])) for answer in answers if answer[0] == socket.AF_INET6}
    if ipv4 != {expected}:
        observed = ', '.join(sorted(ipv4)) or 'no IPv4 address'
        raise RuntimeError(f'DNS A record for {hostname} resolves to {observed}; this direct VPS installation requires only {expected}. Correct the A record or DNS proxy setting, wait for propagation, and rerun.')
    warnings = []
    if ipv6:
        assigned = local_ipv6_addresses()
        if assigned is not None and ipv6 - assigned:
            conflicts = ', '.join(sorted(ipv6 - assigned))
            warnings.append(f'{hostname} has AAAA records ({conflicts}) that are not assigned to this server. Correct or remove those AAAA records so IPv6 clients and certificate validation reach this VPS.')
    return {'ipv4': sorted(ipv4), 'ipv6': sorted(ipv6), 'warnings': warnings}

def memory_preflight():
    try:
        fields = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines() if ':' in line)
        total = int(fields['MemTotal'].split()[0]) * 1024
    except (OSError, KeyError, ValueError):
        return []
    if total < 3 * 1024**3:
        return [f'Only {total / 1024**3:.1f} GiB RAM is detected. Provision at least 4 GiB for the full staging stack and restore drills before running larger workloads.']
    return []

def wait_for_https_health(hostname, attempts=24, interval=5, request_timeout=10):
    """Bounded, authenticated-TLS checks; a maintenance response never means ready."""
    if attempts < 1 or interval < 0 or request_timeout <= 0:
        raise ValueError('Health retry values must be positive.')
    url = f'https://{hostname}/healthz'
    context = ssl.create_default_context()
    last_failure = 'no response'
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'FireISP-Installer/0.1'})
            with urllib.request.urlopen(request, timeout=request_timeout, context=context) as response:
                target = urllib.parse.urlsplit(response.geturl())
                if target.scheme != 'https' or target.hostname != hostname or target.port not in (None, 443) or target.path != '/healthz':
                    raise ValueError('The health request redirected away from the requested HTTPS endpoint.')
                if response.status != 200:
                    raise ValueError('The health endpoint did not return HTTP 200.')
                body = response.read(16385)
                if len(body) > 16384:
                    raise ValueError('The health response exceeded its size limit.')
                payload = json.loads(body)
                if not isinstance(payload, dict) or payload.get('application_ready') is not True or payload.get('database_ready') is not True:
                    raise ValueError('The application or database is not ready.')
            return {'url': url, 'tls_verified': True, 'application_ready': True, 'database_ready': True}
        except urllib.error.HTTPError as exc:
            last_failure = f'HTTP {exc.code}'
        except urllib.error.URLError as exc:
            last_failure = 'TLS certificate verification failed' if isinstance(exc.reason, ssl.SSLError) else 'connection or DNS lookup failed'
        except ssl.SSLError:
            last_failure = 'TLS certificate verification failed'
        except (OSError, TimeoutError):
            last_failure = 'connection timed out or failed'
        except (ValueError, UnicodeError):
            last_failure = 'health response was invalid or application/database was not ready'
        if attempt + 1 < attempts:
            time.sleep(interval)
    raise RuntimeError(f'HTTPS health verification failed for {url} after {attempts} checks ({last_failure}). Verify DNS A/AAAA records, provider inbound TCP 80/443, and the Caddy/web service logs. Fix the reported prerequisite and rerun the installer; existing credentials remain preserved.')

def ensure_docker():
    """Install the container runtime shared by main and additional-node installers."""
    compose_available = bool(shutil.which('docker')) and subprocess.run(['docker', 'compose', 'version'], capture_output=True).returncode == 0
    if not compose_available:
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', 'ca-certificates', 'curl', 'gnupg')
        Path('/etc/apt/keyrings').mkdir(exist_ok=True, mode=0o755)
        run('curl', '-fsSL', 'https://download.docker.com/linux/ubuntu/gpg', '-o', '/etc/apt/keyrings/docker.asc')
        os.chmod('/etc/apt/keyrings/docker.asc', 0o644)
        arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
        Path('/etc/apt/sources.list.d/docker.sources').write_text(f'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\nArchitectures: {arch}\nSigned-By: /etc/apt/keyrings/docker.asc\n')
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin')
    run('systemctl', 'enable', '--now', 'docker')
    run('docker', 'compose', 'version')
    version = subprocess.check_output(['docker', 'version', '--format', '{{.Server.Version}}'], text=True).strip()
    if not re.match(r'^[0-9]+\.', version) or int(version.split('.')[0]) < 28:
        raise RuntimeError('Docker Engine 28 or newer is required before publishing private loopback services. Upgrade Docker and rerun.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--hostname', required=True)
    parser.add_argument('--public-ip', required=True)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--demo-data', action='store_true')
    parser.add_argument('--release', help='Full commit SHA when installing an exported source archive')
    parser.add_argument('--local-workers', help='Local roles: billing,fiscal,network. Defaults to all on first install; preserves placement on reruns.')
    options = parser.parse_args()
    if os.geteuid() != 0: parser.error('Run the installer as root.')
    if len(options.hostname) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in options.hostname.split('.')): parser.error('Invalid hostname.')
    ipaddress.IPv4Address(options.public_ip)
    if not supported_ubuntu(Path('/etc/os-release').read_text()): parser.error('This installer supports Ubuntu 24.04 only.')
    options.source = options.source.resolve()
    try:
        release = release_identity(options.source, options.release)
        staging = Path('/opt/fireisp/staging')
        env_file = staging / '.env'
        previous_environment = env_file.read_text() if env_file.exists() else ''
        profiles = local_profiles(options.local_workers, previous_environment)
        effective_profiles = profiles if profiles is not None else existing_profiles(previous_environment)
    except ValueError as exc:
        parser.error(str(exc))
    for name in ('Dockerfile', 'deploy/backup.py', 'deploy/check_caddy.py', 'deploy/staging/Dockerfile.caddy', 'deploy/staging/compose.yaml', 'deploy/staging/Caddyfile', 'deploy/staging/postgres-init.sh', 'deploy/fireisp-backup.service', 'deploy/fireisp-backup.timer'):
        if not (options.source / name).is_file(): parser.error('The source checkout is incomplete.')
    if shutil.disk_usage('/').free < 5 * 1024**3: parser.error('At least 5 GiB of free storage is required.')
    try:
        dns = dns_preflight(options.hostname, options.public_ip)
    except RuntimeError as exc:
        parser.error(str(exc))
    for warning in dns['warnings'] + memory_preflight():
        print('Preflight: ' + warning, file=sys.stderr)
    ensure_docker()
    if not shutil.which('age'):
        run('apt-get', 'update')
        run('apt-get', 'install', '-y', 'age')
    if 'network' in effective_profiles.split(','):
        run('modprobe', 'ppp_generic')
        if not Path('/dev/ppp').exists(): run('mknod', '-m', '600', '/dev/ppp', 'c', '108', '0')
    private = Path('/etc/fireisp'); private.mkdir(mode=0o700, exist_ok=True); private.chmod(0o700)
    staging.mkdir(parents=True, exist_ok=True)
    if not env_file.exists():
        app_password = secrets.token_urlsafe(36)
        values = {'STAGING_HOSTNAME': options.hostname, 'FIREISP_SOURCE_DIR': str(options.source.resolve()),
                  'DEBUG': 'false', 'SECRET_KEY': secrets.token_urlsafe(64),
                  'ENCRYPTION_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode(),
                  'POSTGRES_PASSWORD': secrets.token_urlsafe(36), 'FIREISP_DB_PASSWORD': app_password,
                  'DATABASE_URL': f'postgresql://fireisp:{app_password}@db:5432/fireisp',
                  'REDIS_URL': 'redis://redis:6379/0',
                  'ALLOWED_HOSTS': f'{options.hostname},localhost,127.0.0.1,web',
                  'CSRF_TRUSTED_ORIGINS': f'https://{options.hostname}', 'DOCUMENT_ROOT': '/data/documents',
                  'NETWORK_AGENT_SOCKET': '/run/fireisp-network/agent.sock',
                  'NETWORK_RADIUS_TOKEN': secrets.token_urlsafe(48),
                  'NETWORK_PUBLIC_ENDPOINT': options.public_ip, 'FIREISP_VERSION': '0.1.0'}
        write_private(env_file, ''.join(f'{key}={value}\n' for key, value in values.items()))
    placement = {'FIREISP_RELEASE': release}
    if profiles is not None:
        placement['COMPOSE_PROFILES'] = profiles
    update_public_environment(env_file, {'STAGING_HOSTNAME': options.hostname, 'FIREISP_SOURCE_DIR': str(options.source),
        'ALLOWED_HOSTS': f'{options.hostname},localhost,127.0.0.1,web', 'CSRF_TRUSTED_ORIGINS': f'https://{options.hostname}',
        'NETWORK_PUBLIC_ENDPOINT': options.public_ip, **placement})
    for filename in ('compose.yaml', 'Caddyfile', 'postgres-init.sh'):
        source = options.source / 'deploy' / 'staging' / filename
        destination = staging / filename
        if source.resolve() != destination.resolve(): shutil.copyfile(source, destination)
        # Bootstrap uses umask 077. PostgreSQL drops to its own UID before
        # reading init scripts; these templates contain no embedded credentials.
        destination.chmod(0o644)
    compose = ['docker', 'compose', '--project-directory', str(staging), '-f', str(staging / 'compose.yaml')]
    run(*compose, 'config', '--quiet')
    run(*compose, 'build', '--pull')
    caddy_build = subprocess.check_output(compose + ['run', '--rm', '--no-deps', 'caddy', 'caddy', 'build-info'], text=True)
    run(sys.executable, str(options.source / 'deploy/check_caddy.py'), input=caddy_build, text=True)
    run(*compose, 'run', '--rm', '--no-deps', 'caddy', 'caddy', 'validate', '--config', '/etc/caddy/Caddyfile', '--adapter', 'caddyfile')
    current_db = subprocess.check_output(compose + ['ps', '--status', 'running', '-q', 'db'], text=True).strip()
    if current_db:
        run(*compose, 'run', '--rm', '--no-deps', 'web', 'python', '-c',
            "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fireisp.settings'); "
            "import django; django.setup(); from deploy.install import require_remote_executors_drained; "
            f"require_remote_executors_drained(allow_release={release!r})")
    drain_local_executors(compose, effective_profiles)
    run(*compose, 'up', '-d', 'db', 'redis')
    for attempt in range(45):
        status = subprocess.run(compose + ['exec', '-T', 'db', 'pg_isready', '-U', 'postgres'], capture_output=True)
        if status.returncode == 0: break
        time.sleep(2)
    else: raise RuntimeError('Database did not become ready.')
    run(*compose, 'run', '--rm', '--no-deps', 'web', 'python', '-c',
        "import os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fireisp.settings'); "
        "import django; django.setup(); from deploy.install import require_remote_executors_drained; "
        f"require_remote_executors_drained(allow_release={release!r})")
    run(*compose, 'run', '--rm', '--no-deps', 'web', 'python', 'manage.py', 'migrate', '--noinput')
    run(*compose, 'run', '--rm', '--no-deps', 'web', 'python', 'manage.py', 'set_deployment_release', '--release', release)
    run(*compose, 'run', '--rm', '--no-deps', '--user', '0', '-v', '/etc/fireisp:/run/bootstrap', 'web', 'python', 'manage.py', 'bootstrap', '--invitation-file', '/run/bootstrap/first-login.txt', '--url', f'https://{options.hostname}')
    if options.demo_data: run(*compose, 'run', '--rm', '--no-deps', 'web', 'python', 'manage.py', 'seed_demo')
    run(*compose, 'up', '-d', '--remove-orphans')
    # Install the backup tool independently of where the source checkout was cloned.
    write_private(private / 'backup.py', (options.source / 'deploy' / 'backup.py').read_text())
    for filename in ('fireisp-backup.service', 'fireisp-backup.timer'):
        shutil.copyfile(options.source / 'deploy' / filename, Path('/etc/systemd/system') / filename)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'fireisp-backup.timer')
    wait_for_https_health(options.hostname)
    print(f'Installation complete. Verified HTTPS application and database health at https://{options.hostname}/healthz from this server. The first-administrator invitation is protected at /etc/fireisp/first-login.txt; no credentials were printed.')

if __name__ == '__main__': main()
