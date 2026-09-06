import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import MagicMock
import urllib.error

from django.test import SimpleTestCase
from deploy import backup, install


class BackupSafetyTests(SimpleTestCase):
    counts = {'customers': 3, 'invoices': 4, 'payments': 2, 'audit_events': 12, 'migrations': 30}

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'fixture.tar.age'
        self.calls = []
        self.restore_error = False
        self.foreign_label = False
        self.addCleanup(patch.stopall)
        patch.object(backup, 'BACKUPS', self.root).start()
        patch.object(backup.shutil, 'disk_usage', return_value=SimpleNamespace(free=20 * 1024**3)).start()
        patch.object(backup, 'run', side_effect=self.fake_run).start()
        patch.object(backup.subprocess, 'check_output', return_value=json.dumps(self.counts)).start()
        self.health = patch.object(backup, '_health').start()
        patch('builtins.print').start()

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] == ['age', '--decrypt']:
            shutil.copyfile(args[-1], args[args.index('--output') + 1])
        elif args[:2] == ['docker', 'inspect']:
            return subprocess.CompletedProcess(args, 0, stdout='unrelated-container' if self.foreign_label else args[-1])
        elif 'pg_restore' in args and self.restore_error:
            raise subprocess.CalledProcessError(1, args)
        elif args[:2] not in (['docker', 'run'], ['docker', 'exec'], ['docker', 'rm']):
            raise AssertionError('Unexpected external operation in backup test')
        return subprocess.CompletedProcess(args, 0)

    def archive(self, *, corrupt=None, omit_volume=None, unsafe=None, duplicate=False):
        directories = ['config', *backup.VOLUMES]
        files = {'database.dump': b'synthetic database dump', 'config/application.env': b'SECRET_KEY=synthetic-fixture-only\n'}
        for volume in backup.VOLUMES:
            files[volume + '/fixture.txt'] = b'synthetic application state'
        if omit_volume:
            directories.remove(omit_volume)
            files = {key: value for key, value in files.items() if not key.startswith(omit_volume + '/')}
        manifest = {'application': 'fireisp', 'format': 2, 'backup_id': 'a' * 32, 'created_at': '20260905T000000Z',
                    'database_counts': self.counts, 'directories': directories,
                    'files': {name: {'size': len(value), 'sha256': hashlib.sha256(value).hexdigest()} for name, value in files.items()}}
        with tarfile.open(self.source, 'w') as tar:
            for directory in directories:
                member = tarfile.TarInfo(directory)
                member.type = tarfile.DIRTYPE
                tar.addfile(member)
            for name, value in files.items():
                actual = b'X' + value[1:] if name == corrupt else value
                member = tarfile.TarInfo(name)
                member.size = len(actual)
                tar.addfile(member, io.BytesIO(actual))
            if unsafe:
                member = tarfile.TarInfo(unsafe)
                member.type = tarfile.SYMTYPE
                member.linkname = '../../outside'
                tar.addfile(member)
            if duplicate:
                value = files['database.dump']
                member = tarfile.TarInfo('database.dump')
                member.size = len(value)
                tar.addfile(member, io.BytesIO(value))
            data = json.dumps(manifest).encode()
            member = tarfile.TarInfo('manifest.json')
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
        return self.source

    def test_restore_checks_all_files_and_counts_in_disposable_networkless_container(self):
        result = backup.verify_backup(self.archive())
        self.assertEqual(result['counts'], self.counts)
        self.assertEqual(result['files_verified'], 6)
        launch = next(args for args in self.calls if args[:2] == ['docker', 'run'])
        self.assertEqual(launch[launch.index('--network') + 1], 'none')
        for option in ('--publish', '-p', '--volume', '-v', '--mount'):
            self.assertNotIn(option, launch)
        name = launch[launch.index('--name') + 1]
        self.assertTrue(name.startswith('fireisp-restore-'))
        restore = next(args for args in self.calls if 'pg_restore' in args)
        self.assertIn(name, restore)
        self.assertIn('--no-owner', restore)
        self.assertIn('--no-privileges', restore)
        self.assertEqual(restore[restore.index('-d') + 1], 'fireisp_verify')
        self.assertIn(['docker', 'rm', '--force', '--volumes', name], self.calls)
        self.assertFalse(list(self.root.glob('fireisp-restore-*')))

    def test_corrupt_database_and_config_fail_before_starting_a_container(self):
        for name in ('database.dump', 'config/application.env', 'radius_config/fixture.txt'):
            with self.subTest(name=name):
                self.calls.clear()
                with self.assertRaises(ValueError):
                    backup.verify_backup(self.archive(corrupt=name))
                self.assertFalse(any(args[0] == 'docker' for args in self.calls))

    def test_missing_volume_duplicate_and_link_are_rejected_before_restore(self):
        for options in ({'omit_volume': 'documents'}, {'omit_volume': 'radius_config'}, {'duplicate': True}, {'unsafe': 'documents/link'}, {'unsafe': '../outside'}):
            with self.subTest(options=options):
                self.calls.clear()
                with self.assertRaises(ValueError):
                    backup.verify_backup(self.archive(**options))
                self.assertFalse(any(args[0] == 'docker' for args in self.calls))

    def test_restore_failure_still_removes_only_the_owned_container_and_volume(self):
        self.restore_error = True
        with self.assertRaises(subprocess.CalledProcessError):
            backup.verify_backup(self.archive())
        launches = [args for args in self.calls if args[:2] == ['docker', 'run']]
        name = launches[0][launches[0].index('--name') + 1]
        self.assertIn(['docker', 'rm', '--force', '--volumes', name], self.calls)
        self.health.assert_not_called()

    def test_row_count_mismatch_is_not_reported_as_verified(self):
        changed = {**self.counts, 'payments': 0}
        with patch.object(backup.subprocess, 'check_output', return_value=json.dumps(changed)):
            with self.assertRaises(ValueError):
                backup.verify_backup(self.archive())
        self.assertTrue(any(args[:2] == ['docker', 'rm'] for args in self.calls))
        self.health.assert_not_called()

    def test_cleanup_refuses_container_with_different_ownership_label(self):
        self.foreign_label = True
        with self.assertRaises(RuntimeError):
            backup.verify_backup(self.archive())
        self.assertFalse(any(args[:2] == ['docker', 'rm'] for args in self.calls))

    def test_low_space_preserves_existing_backups_and_does_not_prune(self):
        existing = self.root / 'existing-backup.tar.age'
        existing.write_bytes(b'preserve this file')
        with patch.object(backup, '_sources', return_value=({}, set())), patch.object(backup.subprocess, 'check_output', return_value='1000'), patch.object(backup.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)), patch.object(backup, 'prune_backups') as prune:
            with self.assertRaises(RuntimeError):
                backup.make_backup()
            prune.assert_not_called()
        self.assertEqual(existing.read_bytes(), b'preserve this file')

    def test_accounting_backup_freezes_complete_prefix_while_writer_appends(self):
        journal = self.root / '20260905.detail'
        complete = b'Sat Sep  5 18:00:00 2026\n\tTimestamp = 1788652800\n\n'
        partial = b'Sat Sep  5 18:01:00 2026\n\tTimestamp = '
        journal.write_bytes(complete + partial)
        archive_name = backup.JOURNAL_PREFIX + journal.name
        archive = self.root / 'journal.tar'
        with tarfile.open(archive, 'w') as tar:
            addfile = tar.addfile
            def append_during_capture(member, reader):
                addfile(member, reader)
                with journal.open('ab') as writer:
                    writer.write(b'1788652860\n\n')
            with patch.object(tar, 'addfile', side_effect=append_during_capture):
                metadata = backup._add_file(tar, journal, archive_name)
        with tarfile.open(archive) as tar:
            self.assertEqual(tar.extractfile(archive_name).read(), complete)
        self.assertEqual(metadata['sha256'], hashlib.sha256(complete).hexdigest())
        self.assertTrue(metadata['complete_journal_prefix'])
        self.assertTrue(journal.read_bytes().endswith(b'1788652860\n\n'))

    def test_accounting_backup_rejects_rewritten_prefix_and_oversized_partial_record(self):
        journal = self.root / '20260905.detail'
        original = b'Sat Sep  5 18:00:00 2026\n\tTimestamp = 1788652800\n\n'
        journal.write_bytes(original)
        with tarfile.open(self.root / 'changed.tar', 'w') as tar:
            addfile = tar.addfile
            def rewrite_during_capture(member, reader):
                addfile(member, reader)
                journal.write_bytes(b'X' + original[1:])
            with patch.object(tar, 'addfile', side_effect=rewrite_during_capture):
                with self.assertRaisesRegex(ValueError, 'prefix changed'):
                    backup._add_file(tar, journal, backup.JOURNAL_PREFIX + journal.name)
        journal.write_bytes(original + b'X' * (backup.JOURNAL_MAX_BLOCK + 1))
        with tarfile.open(self.root / 'incomplete.tar', 'w') as tar:
            with self.assertRaisesRegex(ValueError, 'oversized incomplete'):
                backup._add_file(tar, journal, backup.JOURNAL_PREFIX + journal.name)

    def test_archived_replay_checkpoint_restarts_delivery_without_changing_live_cursor(self):
        cursor = self.root / '.replay-cursor.json'
        live = b'{"format":1,"files":{"synthetic-live-position":1234}}'
        cursor.write_bytes(live)
        archive_name = backup.JOURNAL_PREFIX + cursor.name
        archive = self.root / 'cursor.tar'
        with tarfile.open(archive, 'w') as tar:
            metadata = backup._add_file(tar, cursor, archive_name)
        with tarfile.open(archive) as tar:
            restored = tar.extractfile(archive_name).read()
        self.assertEqual(json.loads(restored), {'format': 1, 'files': {}})
        self.assertEqual(metadata['sha256'], hashlib.sha256(restored).hexdigest())
        self.assertTrue(metadata['replay_cursor_reset_for_restore'])
        self.assertEqual(cursor.read_bytes(), live)

    def pair(self, timestamp, corrupt=False):
        stamp = timestamp.strftime('%Y%m%dT%H%M%SZ')
        path = self.root / f'fireisp-{stamp}.tar.age'
        path.write_bytes(b'synthetic encrypted fixture')
        metadata = {'application': 'fireisp', 'format': 2, 'backup_id': 'a' * 32, 'created_at': stamp, 'bytes': path.stat().st_size,
                    'encrypted_sha256': '0' * 64 if corrupt else backup.sha256(path)}
        path.with_suffix('.json').write_text(json.dumps(metadata))
        return path

    def test_retention_keeps_recent_and_daily_points_and_never_deletes_unverified_pairs(self):
        now = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
        newest = self.pair(now)
        previous = self.pair(now - timedelta(minutes=15))
        extra_today = self.pair(now - timedelta(hours=1))
        yesterday = self.pair(now - timedelta(days=1))
        old = self.pair(now - timedelta(days=3))
        corrupt = self.pair(now - timedelta(days=4), corrupt=True)
        unknown = self.root / 'fireisp-20260101T000000Z.tar.age'
        unknown.write_bytes(b'no ownership metadata')
        backup.prune_backups(keep_latest=2, daily_days=2, now=now)
        for path in (newest, previous, yesterday, corrupt, unknown):
            self.assertTrue(path.exists(), path.name)
        for path in (extra_today, old):
            self.assertFalse(path.exists(), path.name)
            self.assertFalse(path.with_suffix('.json').exists())


class InstallerSafetyTests(SimpleTestCase):
    def test_private_writer_refuses_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, link = root / 'target', root / 'link'
            target.write_text('preserve')
            link.symlink_to(target)
            with self.assertRaises(OSError):
                install.write_private(link, 'replacement')
            self.assertEqual(target.read_text(), 'preserve')

    def test_rerun_updates_public_config_without_rotating_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('SECRET_KEY=synthetic-fixture\nDATABASE_URL=synthetic-private-value\nFIREISP_SOURCE_DIR=/old\n')
            install.update_public_environment(path, {'FIREISP_SOURCE_DIR': '/new checkout', 'STAGING_HOSTNAME': 'example.test'})
            content = path.read_text()
            self.assertIn('SECRET_KEY=synthetic-fixture\n', content)
            self.assertIn('DATABASE_URL=synthetic-private-value\n', content)
            self.assertNotIn('/old', content)
            self.assertIn("FIREISP_SOURCE_DIR='/new checkout'", content)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_noble_repository_is_not_used_on_an_unsupported_release(self):
        self.assertTrue(install.supported_ubuntu('ID=ubuntu\nVERSION_ID="24.04"\n'))
        self.assertFalse(install.supported_ubuntu('ID=ubuntu\nVERSION_ID="22.04"\n'))
        self.assertFalse(install.supported_ubuntu('ID=debian\nVERSION_ID="12"\nPRETTY_NAME="Ubuntu compatible"\n'))

    def test_dns_requires_only_the_intended_ipv4_before_installation(self):
        answer = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.10', 443))
        with patch.object(install.socket, 'getaddrinfo', return_value=[answer, answer]):
            result = install.dns_preflight('isp.example', '192.0.2.10')
            self.assertEqual(result['ipv4'], ['192.0.2.10'])
            self.assertEqual(result['warnings'], [])
        other = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.20', 443))
        with patch.object(install.socket, 'getaddrinfo', return_value=[answer, other]):
            with self.assertRaisesRegex(RuntimeError, 'Correct the A record'):
                install.dns_preflight('isp.example', '192.0.2.10')
        with patch.object(install.socket, 'getaddrinfo', side_effect=socket.gaierror('fixture DNS failure')):
            with self.assertRaisesRegex(RuntimeError, 'Create an A record'):
                install.dns_preflight('isp.example', '192.0.2.10')

    def test_ipv6_warning_requires_an_observed_conflicting_aaaa_record(self):
        ipv4 = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('192.0.2.10', 443))
        ipv6 = (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('2001:db8::10', 443, 0, 0))
        with patch.object(install.socket, 'getaddrinfo', return_value=[ipv4]), patch.object(install, 'local_ipv6_addresses') as local:
            self.assertEqual(install.dns_preflight('isp.example', '192.0.2.10')['warnings'], [])
            local.assert_not_called()
        with patch.object(install.socket, 'getaddrinfo', return_value=[ipv4, ipv6]), patch.object(install, 'local_ipv6_addresses', return_value={'2001:db8::10'}):
            self.assertEqual(install.dns_preflight('isp.example', '192.0.2.10')['warnings'], [])
        with patch.object(install.socket, 'getaddrinfo', return_value=[ipv4, ipv6]), patch.object(install, 'local_ipv6_addresses', return_value={'2001:db8::20'}):
            warnings = install.dns_preflight('isp.example', '192.0.2.10')['warnings']
            self.assertEqual(len(warnings), 1)
            self.assertIn('Correct or remove', warnings[0])

    def response(self, payload, url='https://isp.example/healthz'):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.geturl.return_value = url
        response.read.return_value = json.dumps(payload).encode()
        return response

    def test_https_waits_for_real_application_and_database_readiness_with_verified_tls(self):
        maintenance = self.response({'application_ready': False, 'database_ready': True})
        ready = self.response({'application_ready': True, 'database_ready': True})
        with patch.object(install.urllib.request, 'urlopen', side_effect=[maintenance, ready]) as request, patch.object(install.time, 'sleep') as sleep:
            result = install.wait_for_https_health('isp.example', attempts=3, interval=0.1)
        self.assertTrue(result['tls_verified'])
        self.assertEqual(request.call_count, 2)
        context = request.call_args.kwargs['context']
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        sleep.assert_called_once_with(0.1)

    def test_tls_failure_is_bounded_actionable_and_does_not_print_exception_contents(self):
        error = urllib.error.URLError(ssl.SSLCertVerificationError('private diagnostic fixture'))
        with patch.object(install.urllib.request, 'urlopen', side_effect=error) as request, patch.object(install.time, 'sleep'):
            with self.assertRaises(RuntimeError) as caught:
                install.wait_for_https_health('isp.example', attempts=2, interval=0)
        self.assertEqual(request.call_count, 2)
        self.assertIn('TLS certificate verification failed', str(caught.exception))
        self.assertIn('provider inbound TCP 80/443', str(caught.exception))
        self.assertNotIn('private diagnostic fixture', str(caught.exception))

    def test_redirect_or_string_readiness_does_not_complete_installation(self):
        for response in (self.response({'application_ready': 'true', 'database_ready': True}), self.response({'application_ready': True, 'database_ready': True}, url='http://isp.example/healthz'), self.response({'application_ready': True, 'database_ready': True}, url='https://another.example/healthz')):
            with patch.object(install.urllib.request, 'urlopen', return_value=response):
                with self.assertRaises(RuntimeError):
                    install.wait_for_https_health('isp.example', attempts=1)
