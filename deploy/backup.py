#!/usr/bin/env python3
"""Encrypted, hashed backups and restore drills in a disposable PostgreSQL container."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid

STAGING = Path('/opt/fireisp/staging')
BACKUPS = Path('/var/backups/fireisp')
KEY = Path('/etc/fireisp/backup.agekey')
COMPOSE = ['docker', 'compose', '--project-directory', str(STAGING)]
VOLUMES = ('documents', 'network_state', 'radius_config', 'radius_accounting')
MAX_MANIFEST = 32 * 1024**2
MAX_CONTENT = 100 * 1024**3
MIN_FREE = 512 * 1024**2
JOURNAL_PREFIX = 'radius_accounting/fireisp-accounting/'
JOURNAL_FILE = re.compile(re.escape(JOURNAL_PREFIX) + r'[0-9]{8}\.detail\Z')
JOURNAL_TEMP = re.compile(re.escape(JOURNAL_PREFIX) + r'\.replay-cursor\.[0-9a-f]{32}\.tmp\Z')
JOURNAL_MAX_BLOCK = 32768
COUNTS_SQL = "SELECT json_build_object('customers',(SELECT count(*) FROM core_customer),'invoices',(SELECT count(*) FROM billing_invoice),'payments',(SELECT count(*) FROM billing_payment),'audit_events',(SELECT count(*) FROM core_auditevent),'migrations',(SELECT count(*) FROM django_migrations))"


def run(args, **kwargs):
    kwargs.setdefault('check', True)
    return subprocess.run(args, **kwargs)


def database(*args, **kwargs):
    return run(COMPOSE + ['exec', '-T', 'db'] + list(args), **kwargs)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def volume_path(name):
    return Path(subprocess.check_output(['docker', 'volume', 'inspect', f'fireisp-staging_{name}', '--format', '{{.Mountpoint}}'], text=True).strip())


def _regular(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError('Backup source must be a regular file.')


def _sources():
    files = {'config/application.env': STAGING / '.env'}
    directories = {'config'}
    _regular(files['config/application.env'])
    for name in VOLUMES:
        root = volume_path(name)
        if root.is_symlink() or not root.is_dir():
            raise ValueError('Required backup volume is unavailable.')
        directories.add(name)
        for current, child_dirs, child_files in os.walk(root, followlinks=False):
            current = Path(current)
            for child in sorted(child_dirs):
                path = current / child
                if path.is_symlink():
                    raise ValueError('Links in backup volumes require explicit review.')
                directories.add(str(PurePosixPath(name) / path.relative_to(root).as_posix()))
            for child in sorted(child_files):
                path = current / child
                archive_name = str(PurePosixPath(name) / path.relative_to(root).as_posix())
                # Atomic replay checkpoints are reconstructible, and temporary
                # files can disappear during enumeration. Only this exact private
                # temporary filename class is excluded from the inventory.
                if JOURNAL_TEMP.fullmatch(archive_name):
                    continue
                _regular(path)
                files[archive_name] = path
    if len(files) + len(directories) > 250000:
        raise ValueError('Backup file count exceeds the configured bound.')
    return files, directories


@contextmanager
def database_snapshot():
    """Keep one read-only snapshot for pg_dump and its expected row counts."""
    command = COMPOSE + ['exec', '-T', 'db', 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', 'postgres', '-d', 'fireisp']
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        counts_expression = COUNTS_SQL.removeprefix('SELECT ')
        process.stdin.write("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT json_build_object('snapshot',pg_export_snapshot(),'counts'," + counts_expression + ");\n")
        process.stdin.flush()
        if not select.select([process.stdout], [], [], 30)[0]:
            raise RuntimeError('Timed out establishing the backup database snapshot.')
        metadata = json.loads(process.stdout.readline())
        if not re.fullmatch(r'[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+', metadata['snapshot']):
            raise ValueError('Invalid database snapshot identifier.')
        yield metadata
    finally:
        if process.poll() is None:
            try:
                process.stdin.write('ROLLBACK;\n\\q\n')
                process.stdin.flush()
                process.communicate(timeout=10)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                process.kill()
                process.communicate(timeout=5)
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()


class HashingReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        return data


def _complete_journal_prefix(source, size):
    """Freeze the original length at a complete detail-record boundary."""
    if not size:
        return 0
    start = max(0, size - 2 * JOURNAL_MAX_BLOCK)
    source.seek(start)
    tail = source.read(size - start)
    boundaries = list(re.finditer(rb'\r?\n[ \t]*\r?\n', tail))
    prefix = start + boundaries[-1].end() if boundaries else 0
    if size - prefix > JOURNAL_MAX_BLOCK:
        raise ValueError('Accounting journal has an oversized incomplete record.')
    source.seek(0)
    return prefix


def _add_file(tar, path, name):
    if name == JOURNAL_PREFIX + '.replay-cursor.json':
        # The database snapshot precedes journal capture. A later live cursor
        # could skip packets absent from that snapshot after recovery. Replaying
        # the retained records is safe because accounting is idempotent.
        content = b'{"format":1,"files":{}}'
        member = tarfile.TarInfo(name)
        member.mode, member.size = 0o600, len(content)
        tar.addfile(member, io.BytesIO(content))
        return {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest(), 'replay_cursor_reset_for_restore': True}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Nonregular backup source.')
        journal = bool(JOURNAL_FILE.fullmatch(name))
        size = _complete_journal_prefix(source, before.st_size) if journal else before.st_size
        member = tarfile.TarInfo(name)
        member.mode, member.size, member.mtime = 0o600, size, int(before.st_mtime)
        reader = HashingReader(source)
        tar.addfile(member, reader)
        after = os.fstat(source.fileno())
        if journal:
            if after.st_size < before.st_size:
                raise ValueError('An accounting journal was truncated during backup.')
            # Appending is permitted, modification of the archived prefix is not.
            source.seek(0)
            digest, remaining = hashlib.sha256(), size
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError('Accounting journal prefix became unavailable.')
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != reader.digest.hexdigest():
                raise ValueError('Accounting journal prefix changed during backup.')
            return {'size': size, 'sha256': reader.digest.hexdigest(), 'complete_journal_prefix': True, 'source_size_at_capture': before.st_size}
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('A source changed while it was being archived; retry the backup.')
        return {'size': member.size, 'sha256': reader.digest.hexdigest()}


def record_health(code, status, details):
    payload = json.dumps({'code': code, 'status': status, 'details': details})
    run(COMPOSE + ['exec', '-T', 'web', 'python', 'manage.py', 'record_health'], input=payload.encode(), stdout=subprocess.DEVNULL, timeout=30)


def _health(code, status, details):
    try:
        record_health(code, status, details)
    except (OSError, subprocess.SubprocessError):
        print(json.dumps({'health_recorded': False, 'operation': code}))


def make_backup(keep_latest=96, daily_days=30):
    if keep_latest < 1 or daily_days < 1:
        raise ValueError('Retention values must be positive.')
    files, directories = _sources()
    database_size = int(subprocess.check_output(COMPOSE + ['exec', '-T', 'db', 'psql', '-X', '-At', '-U', 'postgres', '-d', 'fireisp', '-c', "SELECT pg_database_size(current_database());"], text=True).strip())
    source_size = sum(path.stat().st_size for path in files.values())
    required = database_size * 3 + source_size * 2 + MIN_FREE
    if shutil.disk_usage(BACKUPS).free < required:
        raise RuntimeError('Insufficient free space for a complete backup; existing backups were preserved.')
    if KEY.is_symlink():
        raise ValueError('Backup identity must not be a symbolic link.')
    if not KEY.exists():
        run(['age-keygen', '-o', str(KEY)], stderr=subprocess.DEVNULL)
    _regular(KEY)
    KEY.chmod(0o600)
    recipient = subprocess.check_output(['age-keygen', '-y', str(KEY)], text=True).strip()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination = BACKUPS / f'fireisp-{stamp}.tar.age'
    if destination.exists() or destination.with_suffix('.json').exists():
        raise RuntimeError('A backup already exists for this second.')
    with tempfile.TemporaryDirectory(prefix='fireisp-backup-', dir=BACKUPS) as temporary:
        temp = Path(temporary)
        dump = temp / 'database.dump'
        with database_snapshot() as snapshot:
            with dump.open('wb') as stream:
                database('pg_dump', '-U', 'postgres', '-d', 'fireisp', '--snapshot', snapshot['snapshot'], '-Fc', stdout=stream, timeout=1800)
        manifest = {'application': 'fireisp', 'format': 2, 'backup_id': uuid.uuid4().hex, 'created_at': stamp,
                    'database_counts': snapshot['counts'], 'directories': sorted(directories), 'files': {}}
        archive = temp / 'snapshot.tar'
        with tarfile.open(archive, 'w') as tar:
            for name in sorted(directories):
                member = tarfile.TarInfo(name)
                member.type, member.mode = tarfile.DIRTYPE, 0o700
                tar.addfile(member)
            for name, path in sorted({'database.dump': dump, **files}.items()):
                manifest['files'][name] = _add_file(tar, path, name)
            metadata = json.dumps(manifest, sort_keys=True).encode()
            if len(metadata) > MAX_MANIFEST:
                raise ValueError('Backup manifest exceeds the configured bound.')
            member = tarfile.TarInfo('manifest.json')
            member.size, member.mode = len(metadata), 0o600
            tar.addfile(member, io.BytesIO(metadata))
        encrypted = temp / 'snapshot.tar.age'
        run(['age', '--encrypt', '--recipient', recipient, '--output', str(encrypted), str(archive)], timeout=1800)
        encrypted.chmod(0o600)
        report = {key: manifest[key] for key in ('application', 'format', 'backup_id', 'created_at')}
        report.update({'encrypted_sha256': sha256(encrypted), 'bytes': encrypted.stat().st_size, 'files': len(manifest['files'])})
        report_path = temp / 'report.json'
        report_path.write_text(json.dumps(report, indent=2))
        report_path.chmod(0o600)
        # Link publication fails if another file already occupies either protected name.
        os.link(encrypted, destination)
        os.link(report_path, destination.with_suffix('.json'))
    prune_backups(keep_latest, daily_days)
    _health('backup', 'ok', {'created_at': stamp, 'encrypted': True, 'bytes': report['bytes'], 'files': report['files']})
    print(json.dumps({'backup': str(destination), 'bytes': report['bytes'], 'encrypted': True}))
    return destination


def validate_archive(tar):
    """Validate every member without extracting archive paths onto the filesystem."""
    members = tar.getmembers()
    if len(members) > 250000:
        raise ValueError('Too many archive members.')
    names = [member.name for member in members]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate archive member names.')
    for member in members:
        path = PurePosixPath(member.name)
        if not member.name or path.is_absolute() or '..' in path.parts or str(path) != member.name or member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) or member.size < 0:
            raise ValueError('Unsafe archive member.')
        if member.name not in ('manifest.json', 'database.dump', 'config', 'config/application.env') and path.parts[0] not in VOLUMES:
            raise ValueError('Unexpected archive member.')
    metadata_member = tar.getmember('manifest.json')
    if not metadata_member.isfile() or metadata_member.size > MAX_MANIFEST:
        raise ValueError('Invalid manifest member.')
    with tar.extractfile(metadata_member) as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict) or manifest.get('application') != 'fireisp' or manifest.get('format') != 2:
        raise ValueError('Unsupported backup format; retain legacy archives for a separately reviewed recovery.')
    actual_files = {member.name for member in members if member.isfile() and member.name != 'manifest.json'}
    actual_dirs = {member.name for member in members if member.isdir()}
    expected_files = manifest.get('files', {})
    expected_dirs = manifest.get('directories', [])
    if not isinstance(expected_files, dict) or not isinstance(expected_dirs, list) or not all(isinstance(name, str) for name in expected_dirs) or actual_files != set(expected_files) or actual_dirs != set(expected_dirs):
        raise ValueError('Backup inventory is incomplete or inconsistent.')
    if not {'database.dump', 'config/application.env'}.issubset(actual_files) or not set(VOLUMES).issubset(actual_dirs):
        raise ValueError('Required configuration or data volumes are missing.')
    total = sum(member.size for member in members)
    if total > MAX_CONTENT:
        raise ValueError('Backup content exceeds the configured bound.')
    for name, expected in expected_files.items():
        member = tar.getmember(name)
        if not isinstance(expected, dict) or expected.get('size') != member.size or not re.fullmatch(r'[0-9a-f]{64}', str(expected.get('sha256', ''))):
            raise ValueError('Invalid file metadata in backup.')
        with tar.extractfile(member) as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected['sha256']:
            raise ValueError('Backup file checksum does not match.')
    counts = manifest.get('database_counts', {})
    if not isinstance(counts, dict) or set(counts) != {'customers', 'invoices', 'payments', 'audit_events', 'migrations'} or any(type(value) is not int or value < 0 for value in counts.values()) or counts['migrations'] == 0:
        raise ValueError('Missing database snapshot row counts.')
    return manifest


def verify_backup(path):
    """Restore only inside a new container with no network, host mounts or published ports."""
    path = Path(path)
    _regular(path)
    if path.stat().st_size > MAX_CONTENT or shutil.disk_usage(BACKUPS).free < path.stat().st_size * 3 + MIN_FREE:
        raise RuntimeError('Insufficient space or oversized restore archive.')
    name = 'fireisp-restore-' + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix='fireisp-restore-', dir=BACKUPS) as temporary:
        temp = Path(temporary)
        archive, dump = temp / 'snapshot.tar', temp / 'database.dump'
        run(['age', '--decrypt', '--identity', str(KEY), '--output', str(archive), str(path)], timeout=1800)
        with tarfile.open(archive, 'r') as tar:
            manifest = validate_archive(tar)
            with tar.extractfile('database.dump') as source, dump.open('wb') as target:
                shutil.copyfileobj(source, target)
        try:
            run(['docker', 'run', '--detach', '--network', 'none', '--name', name,
                 '--label', 'io.fireisp.restore=' + name, '--memory', '600m', '--cpus', '1',
                 '--security-opt', 'no-new-privileges:true', '--cap-drop', 'ALL',
                 '--cap-add', 'CHOWN', '--cap-add', 'DAC_OVERRIDE', '--cap-add', 'FOWNER', '--cap-add', 'SETGID', '--cap-add', 'SETUID',
                 '--env', 'POSTGRES_HOST_AUTH_METHOD=trust', '--env', 'POSTGRES_DB=fireisp_verify',
                 'postgres:17-bookworm', 'postgres', '-c', 'shared_buffers=64MB', '-c', 'max_connections=10'], stdout=subprocess.DEVNULL, timeout=120)
            for attempt in range(45):
                result = run(['docker', 'exec', name, 'pg_isready', '-U', 'postgres', '-d', 'fireisp_verify'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Restore container did not become ready.')
            with dump.open('rb') as stream:
                run(['docker', 'exec', '-i', name, 'pg_restore', '--exit-on-error', '--no-owner', '--no-privileges', '-U', 'postgres', '-d', 'fireisp_verify'], stdin=stream, stdout=subprocess.DEVNULL, timeout=1800)
            result = subprocess.check_output(['docker', 'exec', name, 'psql', '-X', '-At', '-U', 'postgres', '-d', 'fireisp_verify', '-c', COUNTS_SQL + ';'], text=True, timeout=60)
            counts = json.loads(result)
            if counts != manifest['database_counts']:
                raise ValueError('Restored row counts differ from the original database snapshot.')
        finally:
            inspection = run(['docker', 'inspect', '--format', '{{ index .Config.Labels "io.fireisp.restore" }}', name], check=False, capture_output=True, text=True, timeout=30)
            if inspection.returncode == 0 and inspection.stdout.strip() == name:
                run(['docker', 'rm', '--force', '--volumes', name], stdout=subprocess.DEVNULL, timeout=60)
            elif inspection.returncode == 0:
                raise RuntimeError('Refusing to remove a container without the expected ownership label.')
        result = {'restore_verified': True, 'counts': counts, 'files_verified': len(manifest['files']), 'isolated_container': True, 'active_database_untouched': True}
        _health('restore', 'ok', {'backup': path.name, **result})
        print(json.dumps(result))
        return result


def prune_backups(keep_latest=96, daily_days=30, now=None):
    """Keep recent recovery points and daily history; only delete checksum-verified owned pairs."""
    if keep_latest < 1 or daily_days < 1:
        raise ValueError('Retention values must be positive.')
    now = now or datetime.now(timezone.utc)
    candidates = []
    for path in BACKUPS.glob('fireisp-*.tar.age'):
        match = re.fullmatch(r'fireisp-(\d{8}T\d{6}Z)\.tar\.age', path.name)
        report_path = path.with_suffix('.json')
        if not match or path.is_symlink() or report_path.is_symlink() or not report_path.is_file():
            continue
        try:
            timestamp = datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            if not path.is_file() or path.stat().st_uid != os.geteuid() or report_path.stat().st_uid != os.geteuid() or report_path.stat().st_size > 65536:
                continue
            report = json.loads(report_path.read_text())
            if not isinstance(report, dict) or report.get('application') != 'fireisp' or report.get('format') != 2 or report.get('created_at') != match[1] or not re.fullmatch(r'[0-9a-f]{32}', str(report.get('backup_id', ''))) or report.get('bytes') != path.stat().st_size:
                continue
            candidates.append((timestamp, path, report_path, report))
        except (OSError, ValueError, TypeError):
            continue
    candidates.sort(key=lambda value: value[0], reverse=True)
    keep = {item[1] for item in candidates[:keep_latest]}
    days = set()
    cutoff = now.date() - timedelta(days=daily_days - 1)
    for timestamp, path, _, _ in candidates:
        if timestamp.date() >= cutoff and timestamp.date() not in days:
            keep.add(path)
            days.add(timestamp.date())
    removed = 0
    for _, path, report_path, report in candidates:
        if path not in keep and re.fullmatch(r'[0-9a-f]{64}', str(report.get('encrypted_sha256', ''))) and sha256(path) == report['encrypted_sha256']:
            path.unlink()
            report_path.unlink()
            removed += 1
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['create', 'verify', 'create-and-verify'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--keep-latest', type=int, default=96)
    parser.add_argument('--daily-days', type=int, default=30)
    options = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Root is required for protected backup storage.')
    if not shutil.which('age'):
        parser.error('Install age using the FireISP installer.')
    if options.keep_latest < 1 or options.daily_days < 1:
        parser.error('Retention values must be positive.')
    os.umask(0o077)
    if BACKUPS.is_symlink():
        parser.error('Backup directory must not be a symbolic link.')
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUPS.chmod(0o700)
    descriptor = os.open(BACKUPS / '.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if options.action == 'verify':
                if not options.file:
                    parser.error('--file is required for verification.')
                verify_backup(options.file)
            else:
                path = make_backup(options.keep_latest, options.daily_days)
                if options.action == 'create-and-verify':
                    verify_backup(path)
        except Exception as exc:
            _health('restore' if options.action == 'verify' else 'backup', 'failed', {'error': type(exc).__name__})
            raise


if __name__ == '__main__':
    main()
