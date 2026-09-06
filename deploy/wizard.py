#!/usr/bin/env python3
"""Interactive single-server installation and SSH-connected worker selection."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid

REPOSITORY = 'https://github.com/vothalvino/fireisp.git'
STATE = Path('/etc/fireisp/wizard.json')
RELEASES = Path('/opt/fireisp/installer/releases')
MAIN_ENV = Path('/opt/fireisp/staging/.env')
MAIN_LINK = Path('/opt/fireisp/app')
MODULES = (
    ('billing', 'Billing: renewals and collection jobs'),
    ('fiscal', 'Electronic invoicing: Finkok and PDF generation'),
    ('network', 'Network: router jobs, tunnels and RADIUS'),
    ('worker', 'Core events: notifications and coordination'),
    ('scheduler', 'Scheduler: scheduled jobs, with automatic standby'),
    ('web', 'Additional web application: private listener'),
)


def parse_modules(value, mode):
    available = MODULES[:3] if mode == 'main' else MODULES
    aliases = {str(index): role for index, (role, _) in enumerate(available, 1)}
    aliases.update({role: role for role, _ in available})
    value = value.strip().lower()
    if value == 'all':
        return [role for role, _ in available]
    if value in ('none', '0'):
        if mode != 'main':
            raise ValueError('Select at least one module for an additional server.')
        return []
    tokens = [token.strip() for token in value.split(',')]
    if not value or any(token not in aliases for token in tokens):
        raise ValueError('Use the module numbers or names, separated by commas.')
    selected = {aliases[token] for token in tokens}
    return [role for role, _ in available if role in selected]


def validate_hostname(value):
    value = value.strip().lower()
    if len(value) > 253 or '.' not in value or any(
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
            for label in value.split('.')):
        raise ValueError('Enter a domain such as isp.example.com.')
    return value


def validate_public_ip(value):
    try:
        address = ipaddress.IPv4Address(value.strip())
    except ValueError:
        raise ValueError('Enter the server public IPv4 address.') from None
    if not address.is_global:
        raise ValueError('A public IPv4 address is required.')
    return str(address)


def validate_node_id(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,29}', value):
        raise ValueError('Use up to 30 lowercase letters, digits or hyphens for this server name.')
    if value == 'primary':
        raise ValueError('Choose a name other than primary for an additional server.')
    return value


def ask(label, default='', validator=lambda value: value):
    while True:
        answer = input(label + (f' [{default}]' if default else '') + ': ').strip() or default
        try:
            return validator(answer)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)


def read_state(path=STATE):
    if not path.exists() and not path.is_symlink():
        return {}
    if path.is_symlink() or path.stat().st_mode & 0o077 or path.stat().st_uid != os.geteuid():
        raise ValueError('The saved installer settings must be an owner-only regular file.')
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError('The saved installer settings are invalid.')
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or state.get('mode') not in ('main', 'additional'):
        raise ValueError('The saved installation mode is invalid.')
    return state


def save_state(state, path=STATE):
    from deploy.connection import write_private
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, json.dumps(state, indent=2) + '\n')


def main_defaults():
    if not MAIN_ENV.exists():
        return {}
    from deploy.nodes.install import load_environment
    values = load_environment(MAIN_ENV)
    return {'hostname': values.get('STAGING_HOSTNAME', ''),
            'public_ip': values.get('NETWORK_PUBLIC_ENDPOINT', ''),
            'modules': values.get('COMPOSE_PROFILES', 'billing,fiscal,network') or 'none'}


def collect_options(options, previous):
    interactive = not options.no_input
    default_mode = previous.get('mode', 'main' if MAIN_ENV.exists() else '')
    if options.mode:
        mode = options.mode
    elif interactive:
        print('\nFireISP installation\n  1. Main server: application, database and selected local modules\n'
              '  2. Additional server: connect selected modules to the main server')
        def mode_choice(value):
            choices = {'1': 'main', '2': 'additional', 'main': 'main', 'additional': 'additional'}
            if value not in choices:
                raise ValueError('Select 1 for main or 2 for additional server.')
            return choices[value]
        mode = ask('Server type', '2' if default_mode == 'additional' else '1', mode_choice)
    else:
        raise ValueError('--mode is required with --no-input.')
    if previous.get('mode') and previous['mode'] != mode:
        raise ValueError('This server already has a different installation mode. Keep that mode; use another server for the new role.')
    if mode == 'additional' and MAIN_ENV.exists():
        raise ValueError('This is already a main server. Select Main to change its local modules.')
    defaults = main_defaults() if mode == 'main' else previous
    result = {'mode': mode}
    if options.modules is not None:
        result['roles'] = parse_modules(options.modules, mode)
    elif interactive:
        print('\nModules to run on this server:')
        for index, (_, description) in enumerate(MODULES[:3] if mode == 'main' else MODULES, 1):
            print(f'  {index}. {description}')
        if mode == 'main':
            print('  0. Main application only; connect workers later')
            print('The main server always includes customers, operations, compliance, core events and scheduling.')
        selected = defaults.get('modules') or ','.join(previous.get('roles', []))
        if not selected:
            selected = '1,2,3' if mode == 'main' else '2'
        result['roles'] = ask('Modules (comma separated, or all)', selected, lambda value: parse_modules(value, mode))
    else:
        raise ValueError('--modules is required with --no-input.')
    def field(name, label, default='', validator=lambda value: value):
        supplied = getattr(options, name, None)
        if supplied is not None:
            return validator(str(supplied))
        if interactive:
            return ask(label, str(default), validator)
        if default != '':
            return validator(str(default))
        raise ValueError('--' + name.replace('_', '-') + ' is required with --no-input.')
    if mode == 'main':
        result['hostname'] = field('hostname', 'Application domain', defaults.get('hostname', ''), validate_hostname)
        result['public_ip'] = field('public_ip', 'This server public IPv4', defaults.get('public_ip', ''), validate_public_ip)
    else:
        from deploy.connection import validate_host
        proposed = re.sub('[^a-z0-9-]', '-', socket.gethostname().lower().split('.')[0])[:30].strip('-')
        if not proposed or proposed == 'primary':
            proposed = 'worker-1'
        result['node_id'] = field('node_id', 'Name for this additional server', previous.get('node_id', proposed), validate_node_id)
        if previous.get('node_id') and previous['node_id'] != result['node_id']:
            raise ValueError('Keep the existing server name when changing its modules. A new name would leave its current workers running.')
        result['main_host'] = field('main_host', 'Main server SSH hostname or IP', previous.get('main_host', ''), validate_host)
        result['ssh_port'] = int(field('ssh_port', 'Main server SSH port', previous.get('ssh_port', 22), valid_port))
        result['admin_user'] = field('admin_user', 'Main server SSH administrator', previous.get('admin_user', 'root'), valid_user)
        if options.no_input:
            result['admin_key'] = options.admin_key or previous.get('admin_key', '')
        else:
            print('Use an existing SSH key, or leave the key path blank to use SSH password authentication.')
            result['admin_key'] = field('admin_key', 'SSH private key path (optional)', previous.get('admin_key', ''))
        if 'network' in result['roles']:
            result['network_endpoint'] = field('network_endpoint', 'This additional server public IPv4 for router tunnels', previous.get('network_endpoint', ''), validate_public_ip)
    return result


def valid_port(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise ValueError('Enter a port between 1 and 65535.')
    return value


def valid_user(value):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', value):
        raise ValueError('Enter a valid Linux SSH user name.')
    return value


def matching_source(source, release, releases=RELEASES):
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise ValueError('The main server did not provide a full release identity.')
    from deploy.install import release_identity
    if release_identity(source) == release:
        return source
    releases.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination = releases / release
    if destination.is_symlink():
        raise ValueError('The release directory must not be a symlink.')
    if not destination.exists():
        candidate = releases / ('.fetch-' + uuid.uuid4().hex)
        candidate.mkdir(mode=0o700)
        try:
            for args in (['init', '-q'], ['remote', 'add', 'origin', REPOSITORY],
                         ['fetch', '--quiet', '--depth=1', 'origin', release],
                         ['checkout', '--quiet', '--detach', 'FETCH_HEAD']):
                subprocess.run(['git', '-C', str(candidate), *args], check=True)
            if release_identity(candidate) != release:
                raise ValueError('The downloaded source does not match the main server.')
            for path in candidate.rglob('*'):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode & ~0o222)
            candidate.chmod(candidate.stat().st_mode & ~0o222)
            candidate.rename(destination)
        finally:
            if candidate.exists():
                shutil.rmtree(candidate)
    if release_identity(destination) != release:
        raise ValueError('The saved release source does not match the main server.')
    return destination


def publish_main_source(source):
    """Keep the pairing helper at its stable path without deleting earlier source."""
    source = source.resolve()
    if MAIN_LINK.resolve() == source:
        return
    MAIN_LINK.parent.mkdir(parents=True, exist_ok=True)
    candidate = MAIN_LINK.parent / ('.app-link-' + uuid.uuid4().hex)
    candidate.symlink_to(source, target_is_directory=True)
    try:
        if MAIN_LINK.exists() and not MAIN_LINK.is_symlink():
            previous = MAIN_LINK.parent / ('app.previous-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6])
            MAIN_LINK.rename(previous)
        candidate.replace(MAIN_LINK)
    finally:
        candidate.unlink(missing_ok=True)


def wait_for_role(directory, role, node_id, attempts=18):
    check = '''import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fireisp.settings')
import django
django.setup()
from core.models import RuntimeNode
from django.utils import timezone
node = RuntimeNode.objects.filter(identifier=os.environ['FIREISP_NODE_ID'] + ':' + os.environ['FIREISP_NODE_ROLE'], release=os.environ['FIREISP_RELEASE']).first()
sys.exit(0 if node and node.status in ('ready','standby') and (timezone.now()-node.last_seen).total_seconds()<60 else 1)
'''
    command = ['docker', 'compose', '--project-directory', str(directory), '-f', str(directory / 'compose.json'),
               'exec', '-T', role, 'python', '-c', check]
    for attempt in range(attempts):
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            return
        if attempt + 1 < attempts:
            time.sleep(3)
    raise RuntimeError(f'{node_id} has not reported a healthy runtime heartbeat. Check its protected configuration and connection before moving work.')


def install_main(plan, source):
    from deploy.install import release_identity
    release = release_identity(source)
    subprocess.run([sys.executable, str(source / 'deploy/install.py'), '--source', str(source),
                    '--release', release, '--hostname', plan['hostname'], '--public-ip', plan['public_ip'],
                    '--local-workers', ','.join(plan['roles'])], check=True)
    publish_main_source(source)
    print('\nMain server is ready at https://' + plan['hostname'])
    print('Run the same one-line installer on an additional server and choose Additional to connect it here.')


def install_additional(plan, previous, source, *, interactive=True):
    from deploy.install import ensure_docker
    from deploy.connection import connect_main
    ensure_docker()
    if not shutil.which('ssh') or not shutil.which('ssh-keygen'):
        subprocess.run(['apt-get', 'update'], check=True)
        subprocess.run(['apt-get', 'install', '-y', 'openssh-client'], check=True)
    print('\nConnecting to the main server. SSH may ask you to verify its fingerprint and authenticate.')
    print('The main server firewall must allow SSH from this server; database ports remain private.')
    connection = connect_main(plan['main_host'], plan['ssh_port'], plan['admin_user'], plan['admin_key'],
                              plan['node_id'], plan['roles'], plan.get('network_endpoint', ''), interactive=interactive)
    source = matching_source(source, connection['release'])
    for role in plan['roles']:
        identifier = plan['node_id'] + '-' + role
        directory = Path('/opt/fireisp/nodes') / identifier
        print('Installing ' + role + '...')
        subprocess.run([sys.executable, str(source / 'deploy/nodes/install.py'), '--role', role,
                        '--node-id', identifier, '--env-file', str(connection['environment_file']),
                        '--release', connection['release'], '--source', str(source), '--allow-loopback-tunnels'], check=True)
        wait_for_role(directory, role, identifier)
    # Only stop roles recorded as belonging to this same wizard installation.
    if previous.get('node_id') == plan['node_id']:
        for role in set(previous.get('roles', [])) - set(plan['roles']):
            if role not in dict(MODULES):
                raise ValueError('The saved module selection is invalid.')
            directory = Path('/opt/fireisp/nodes') / (plan['node_id'] + '-' + role)
            config = directory / 'compose.json'
            if config.exists():
                subprocess.run(['docker', 'compose', '--project-directory', str(directory), '-f', str(config),
                                'stop', '--timeout', '180'], check=True)
    print('\nSelected modules are connected and reporting their status to the main server.')
    if 'network' in plan['roles']:
        print('The new network node is registered. Assign routers from the main application; existing routers were not moved.')


@contextmanager
def installer_lock(path=Path('/var/lock/fireisp-wizard.lock')):
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another FireISP installer is already running on this server.') from None
        yield


def main():
    sys.dont_write_bytecode = True
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['GIT_OPTIONAL_LOCKS'] = '0'
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['main', 'additional'])
    parser.add_argument('--modules', help='Comma-separated module names or menu numbers; main also accepts none')
    parser.add_argument('--hostname')
    parser.add_argument('--public-ip')
    parser.add_argument('--node-id')
    parser.add_argument('--main-host')
    parser.add_argument('--ssh-port')
    parser.add_argument('--admin-user')
    parser.add_argument('--admin-key', help='Existing SSH key path, never a password')
    parser.add_argument('--network-endpoint')
    parser.add_argument('--no-input', action='store_true', help='Use explicit options; SSH must already have a trusted host key and key authentication')
    options = parser.parse_args()
    try:
        from deploy.install import supported_ubuntu
        if os.geteuid() != 0:
            raise ValueError('Run the installer with sudo or as root.')
        if not supported_ubuntu(Path('/etc/os-release').read_text()):
            raise ValueError('This installer supports Ubuntu 24.04.')
        if not options.no_input and not sys.stdin.isatty():
            raise ValueError('An interactive terminal is required. Run the one-line installer from an SSH terminal.')
        with installer_lock():
            previous = read_state()
            plan = collect_options(options, previous)
            print('\nInstalling ' + plan['mode'] + ' server; local modules: ' + (', '.join(plan['roles']) or 'main application only'))
            source = Path(__file__).resolve().parents[1]
            if plan['mode'] == 'main':
                install_main(plan, source)
            else:
                install_additional(plan, previous, source, interactive=not options.no_input)
            save_state(plan)
    except (EOFError, KeyboardInterrupt):
        print('\nInstallation interrupted. Some steps may already be installed; rerun to finish. The previous saved selection was not replaced.', file=sys.stderr)
        raise SystemExit(130) from None
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    except (OSError, subprocess.SubprocessError):
        parser.error('Installation did not finish. Check the connection and preceding installer result, then rerun; credentials were not printed.')


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main()
