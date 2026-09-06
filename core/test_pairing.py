import base64
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase

from deploy import pairing


def test_key(character=b'x'):
    raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + character * 32
    return 'ssh-ed25519 ' + base64.b64encode(raw).decode()


class PairingValidationTests(SimpleTestCase):
    def setUp(self):
        self.request = {'node_id': 'north', 'roles': ['fiscal'], 'public_key': test_key() + ' node-label'}
        self.values = {
            'SECRET_KEY': 'synthetic-application-secret-' + 'x' * 32,
            'ENCRYPTION_KEY': base64.urlsafe_b64encode(b'x' * 32).decode(),
            'DATABASE_URL': 'postgresql://fireisp:literal%24secret@db:5432/fireisp',
            'REDIS_URL': 'redis://redis:6379/0', 'FIREISP_RELEASE': 'a' * 40,
            'ALLOWED_HOSTS': 'isp.example.test,localhost', 'CSRF_TRUSTED_ORIGINS': 'https://isp.example.test',
            'FIREISP_VERSION': '0.1.0', 'DEBUG': 'true', 'POSTGRES_PASSWORD': 'superuser-secret',
            'FIREISP_DB_PASSWORD': 'duplicate-secret', 'NETWORK_RADIUS_TOKEN': 'legacy-primary-token',
            'UNRELATED_API_SECRET': 'must-stay-on-main',
        }

    def test_pairing_exports_only_application_keys_and_tunnel_endpoints(self):
        release, values = pairing.paired_environment(self.values)
        self.assertEqual(release, 'a' * 40)
        self.assertEqual(set(values), pairing.ENVIRONMENT_KEYS)
        self.assertEqual(values['DATABASE_URL'], 'postgresql://fireisp:literal%24secret@127.0.0.1:15432/fireisp')
        self.assertEqual(values['REDIS_URL'], 'redis://127.0.0.1:16379/0')
        self.assertNotIn('superuser-secret', json.dumps(values))
        self.assertNotIn('legacy-primary-token', json.dumps(values))

    def test_pairing_rejects_unversioned_or_nonstandard_main_configuration(self):
        for changes in ({'FIREISP_RELEASE': 'main'}, {'DATABASE_URL': 'postgresql://postgres:secret@db/fireisp'},
                        {'DATABASE_URL': 'postgresql://fireisp:secret@db/fireisp?host=8.8.8.8'},
                        {'DATABASE_URL': 'postgresql://fireisp:secret@external-db/fireisp'},
                        {'REDIS_URL': 'redis://other-broker/0'}, {'ENCRYPTION_KEY': 'invalid'},
                        {'ALLOWED_HOSTS': '*'}, {'SECRET_KEY': 'short'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                pairing.paired_environment({**self.values, **changes})

    def test_key_comments_cannot_change_authorized_key_options(self):
        request = pairing.validate_request(self.request)
        line = pairing.forwarding_key(request)
        self.assertEqual(request['public_key'], test_key())
        self.assertTrue(line.startswith('restrict,port-forwarding,'))
        self.assertIn('command="/usr/sbin/nologin"', line)
        self.assertIn('permitopen="127.0.0.1:15432"', line)
        self.assertIn('permitopen="127.0.0.1:16379"', line)
        self.assertIn('permitopen="127.0.0.1:18000"', line)
        self.assertTrue(line.endswith(' fireisp:north'))
        self.assertNotIn('node-label', line)

    def test_pairing_rejects_path_option_and_key_injection(self):
        for changes in ({'node_id': '../north'}, {'node_id': '-oProxyCommand=x'}, {'node_id': 'a' * 31},
                        {'roles': ['fiscal', 'fiscal']}, {'roles': ['unknown']}, {'roles': 'fiscal'},
                        {'public_key': 'command="id" ' + test_key()}, {'public_key': test_key() + '\n' + test_key()},
                        {'public_key': 'ssh-ed25519 ' + base64.b64encode(b'not-a-valid-key').decode()},
                        {'public_key': 'ssh-ed25519 !!!'}, {'environment': {'SECRET_KEY': 'override'}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                pairing.validate_request({**self.request, **changes})

    def test_network_registration_requires_public_ipv4_endpoint(self):
        for endpoint in ('127.0.0.1', '10.0.0.1', '::1', '8.8.8.8;id', ''):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                pairing.validate_request({**self.request, 'roles': ['network'], 'network_endpoint': endpoint})
        request = pairing.validate_request({**self.request, 'roles': ['network'], 'network_endpoint': '8.8.8.8'})
        self.assertEqual(request['network_endpoint'], '8.8.8.8')

    def test_reruns_preserve_network_identity_when_role_is_removed_and_readded(self):
        network = pairing.validate_request({**self.request, 'roles': ['network'], 'network_endpoint': '8.8.8.8'})
        initial = pairing.profile_for(network, None)
        removed = pairing.profile_for(pairing.validate_request(self.request), initial)
        self.assertEqual(removed['roles'], ['fiscal'])
        self.assertEqual(removed['network_token'], initial['network_token'])
        self.assertEqual(removed['network_endpoint'], '8.8.8.8')
        restored = pairing.profile_for(network, removed)
        self.assertEqual(restored['network_token'], initial['network_token'])
        with self.assertRaises(ValueError):
            pairing.profile_for({**network, 'public_key': test_key(b'y')}, initial)
        with self.assertRaises(ValueError):
            pairing.profile_for({**network, 'network_endpoint': '1.1.1.1'}, initial)

    def test_running_main_ports_must_match_the_loopback_contract(self):
        compose = ['docker', 'compose']

        def container(service):
            ports = {'db': ('5432/tcp', 15432), 'redis': ('6379/tcp', 16379), 'web': ('8000/tcp', 18000)}
            container_port, host_port = ports[service]
            return {'NetworkSettings': {'Ports': {container_port: [{'HostIp': '127.0.0.1', 'HostPort': str(host_port)}]}},
                    'State': {'Running': True}, 'Config': {'Labels': {'org.opencontainers.image.revision': 'a' * 40}}}

        fixtures = {service: container(service) for service in ('db', 'redis', 'web')}

        def command(arguments):
            if arguments[-3:] == ['config', '--format', 'json']:
                return SimpleNamespace(stdout=json.dumps({'services': {'web': {'environment': self.values}}}))
            if 'ps' in arguments:
                return SimpleNamespace(stdout={'db': 'a', 'redis': 'b', 'web': 'c'}[arguments[-1]] * 64)
            service = {'a': 'db', 'b': 'redis', 'c': 'web'}[arguments[-1][0]]
            return SimpleNamespace(stdout=json.dumps([fixtures[service]]))

        with patch.object(pairing, 'run', side_effect=command):
            self.assertEqual(pairing.main_configuration(compose)[0], 'a' * 40)
            fixtures['db']['NetworkSettings']['Ports']['5432/tcp'][0]['HostIp'] = '0.0.0.0'
            with self.assertRaisesRegex(ValueError, 'loopback'):
                pairing.main_configuration(compose)
            fixtures['db'] = container('db')
            fixtures['web']['Config']['Labels']['org.opencontainers.image.revision'] = 'b' * 40
            with self.assertRaisesRegex(ValueError, 'image'):
                pairing.main_configuration(compose)

    def test_subprocess_errors_do_not_expose_captured_secrets(self):
        error = subprocess.CalledProcessError(1, ['fixture'], output='secret-on-stdout', stderr='secret-on-stderr')
        with patch.object(pairing.subprocess, 'run', side_effect=error):
            with self.assertRaises(ValueError) as result:
                pairing.run(['fixture'])
        self.assertNotIn('secret-on-', str(result.exception))


class PairingFilesystemTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_private_files_reject_symlink_and_world_readable_input(self):
        path = self.directory / 'profile.json'
        with patch.object(pairing.os, 'fchown'):
            pairing.write_private(path, '{"fixture":true}', uid=os.geteuid(), gid=os.getegid())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(pairing.read_private(path, uid=os.geteuid()), '{"fixture":true}')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            pairing.read_private(path, uid=os.geteuid())
        alias = self.directory / 'alias.json'
        alias.symlink_to(path)
        with self.assertRaises(OSError):
            pairing.read_private(alias, uid=os.geteuid())

    def test_sshd_conflicts_restore_previous_configuration(self):
        path = self.directory / 'sshd.conf'
        previous = '# Existing owner-only configuration\n'
        path.write_text(previous)
        path.chmod(0o600)
        with patch.object(pairing, 'read_private', side_effect=lambda path, **kwargs: path.read_text() if path.exists() else None), \
             patch.object(pairing.os, 'fchown'), patch.object(pairing, 'validate_sshd', side_effect=ValueError('conflict')), \
             patch.object(pairing, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'restored'):
                pairing.configure_sshd(path)
        self.assertEqual(path.read_text(), previous)
        run.assert_called_once_with(['systemctl', 'reload', 'ssh'])

    def test_effective_sshd_policy_disallows_remote_forwarding(self):
        values = '''authenticationmethods publickey
passwordauthentication no
kbdinteractiveauthentication no
allowtcpforwarding local
allowstreamlocalforwarding no
permitlisten none
permittty no
permittunnel no
x11forwarding no
allowagentforwarding no
forcecommand /usr/sbin/nologin
permitopen 127.0.0.1:15432 127.0.0.1:16379 127.0.0.1:18000
'''
        with patch.object(pairing, 'run', return_value=SimpleNamespace(stdout=values)):
            pairing.validate_sshd()
        with patch.object(pairing, 'run', return_value=SimpleNamespace(stdout=values.replace('allowtcpforwarding local', 'allowtcpforwarding yes'))):
            with self.assertRaisesRegex(ValueError, 'conflict'):
                pairing.validate_sshd()

    @skipUnless(shutil.which('sshd') and shutil.which('ssh-keygen'), 'OpenSSH server and client are required')
    def test_actual_openssh_include_restrictions_do_not_change_operator_policy(self):
        host_key = self.directory / 'host-key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(host_key)],
                       check=True, capture_output=True, timeout=10)
        rules = self.directory / 'link.conf'
        rules.write_text(pairing.SSHD_RULES)
        config = self.directory / 'sshd_config'
        config.write_text(f'Include {rules}\nHostKey {host_key}\nPidFile {self.directory}/sshd.pid\nPort 22222\nUsePAM no\n')
        subprocess.run([shutil.which('sshd'), '-t', '-f', str(config)], check=True, capture_output=True, timeout=10)
        for user, forwarding in (('fireisp-link', 'local'), ('root', 'yes')):
            result = subprocess.run([shutil.which('sshd'), '-T', '-f', str(config), '-C', f'user={user},host=localhost,addr=127.0.0.1'],
                                    check=True, capture_output=True, text=True, timeout=10)
            self.assertIn(f'allowtcpforwarding {forwarding}\n', result.stdout)
            self.assertIn('forcecommand /usr/sbin/nologin\n' if user == 'fireisp-link' else 'forcecommand none\n', result.stdout)

    def test_invalid_cli_request_never_echoes_input(self):
        with patch.object(pairing.sys, 'argv', ['pairing.py', 'prepare']), \
             patch.object(pairing.sys, 'stdin', io.StringIO('{"sensitive":"DO-NOT-ECHO"}')), \
             patch.object(pairing.sys, 'stderr', new_callable=io.StringIO) as error, \
             patch.object(pairing.os, 'geteuid', return_value=0):
            with self.assertRaises(SystemExit):
                pairing.main()
        self.assertNotIn('DO-NOT-ECHO', error.getvalue())
        self.assertIn('Pairing failed', error.getvalue())

    def test_prepare_retries_reuse_saved_identity_and_do_not_export_deselected_network_token(self):
        directory = self.directory / 'pairings'
        request = {'node_id': 'north', 'roles': ['network'], 'public_key': test_key(), 'network_endpoint': '8.8.8.8'}
        original_stat = os.fstat

        def root_metadata(fd):
            actual = original_stat(fd)
            return SimpleNamespace(st_mode=actual.st_mode, st_uid=0, st_size=actual.st_size)

        def mkdir(path):
            path.mkdir(mode=0o700, exist_ok=True)

        with patch.object(pairing, 'private_directory', side_effect=mkdir), \
             patch.object(pairing.os, 'fstat', side_effect=root_metadata), patch.object(pairing.os, 'fchown'), \
             patch.object(pairing, 'main_configuration', return_value=('a' * 40, {'SECRET_KEY': 'synthetic'})), \
             patch.object(pairing, 'authorize_link') as authorize, patch.object(pairing, 'run') as run:
            first = pairing.prepare(request, state_directory=directory)
            repeated = pairing.prepare(request, state_directory=directory)
            self.assertEqual(first['environment']['NETWORK_RADIUS_TOKEN'], repeated['environment']['NETWORK_RADIUS_TOKEN'])
            removed = pairing.prepare({**request, 'roles': ['fiscal'], 'network_endpoint': ''}, state_directory=directory)
            self.assertNotIn('NETWORK_RADIUS_TOKEN', removed['environment'])
            registration = json.loads(run.call_args.kwargs['input'])
            self.assertNotIn('network_token', registration)
            restored = pairing.prepare(request, state_directory=directory)
            self.assertEqual(first['environment']['NETWORK_RADIUS_TOKEN'], restored['environment']['NETWORK_RADIUS_TOKEN'])
            with self.assertRaisesRegex(ValueError, 'different SSH key'):
                pairing.prepare({**request, 'public_key': test_key(b'y')}, state_directory=directory)
        self.assertEqual(authorize.call_count, 4)
        self.assertEqual(first['ports'], {'database': 15432, 'redis': 16379, 'web': 18000})
        self.assertEqual(first['ssh_user'], 'fireisp-link')
