import base64
import copy
import json
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from deploy import connection
from deploy.nodes.install import load_environment


class ConnectionTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.manifest = {
            'schema': 1, 'release': 'a' * 40, 'node_id': 'north', 'ssh_user': 'fireisp-link',
            'ports': dict(connection.MAIN_PORTS),
            'environment': {
                'SECRET_KEY': 'synthetic-application-secret-$HOME-$(id)-long-value',
                'ENCRYPTION_KEY': base64.urlsafe_b64encode(b'x' * 32).decode(),
                'DATABASE_URL': 'postgresql://fireisp:percent%25colon%3Aat%40@127.0.0.1:15432/fireisp',
                'REDIS_URL': 'redis://:redis%40fixture@127.0.0.1:16379/0',
                'ALLOWED_HOSTS': 'main.example.test,localhost',
                'CSRF_TRUSTED_ORIGINS': 'https://main.example.test',
            },
        }

    def test_host_and_node_inputs_cannot_be_ssh_arguments_or_shell_text(self):
        for host in ('-oProxyCommand=id', 'user@main.example.test', 'main;id', 'https://main.example.test',
                     'main.example.test/path', 'main\nHost other', 'fe80::1%eth0', 'a' * 64 + '.test'):
            with self.subTest(host=host), self.assertRaises(ValueError):
                connection.validate_identity(host, 22, 'root', 'north', ['fiscal'])
        for host in ('main.example.test', '74.208.202.126', '2001:db8::1'):
            self.assertEqual(connection.validate_host(host), host)
        for changes in ({'ssh_port': True}, {'ssh_port': 0}, {'admin_user': 'root;id'},
                        {'node_id': '../north'}, {'node_id': 'a' * 31}, {'roles': []},
                        {'roles': ['fiscal', 'fiscal']}, {'roles': ['database']}):
            options = {'main_host': 'main.example.test', 'ssh_port': 22, 'admin_user': 'root',
                       'node_id': 'north', 'roles': ['fiscal']}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                connection.validate_identity(**(options | changes))

    def test_network_enrollment_requires_its_own_public_endpoint(self):
        for endpoint in ('', '127.0.0.1', '10.0.0.1', '2001:db8::1', 'not-an-address'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                connection.validate_identity('main.test', 22, 'root', 'north', ['network'], endpoint)
        connection.validate_identity('main.test', 22, 'root', 'north', ['network'], '8.8.8.8')

    def test_manifest_rejects_wrong_identity_release_ports_and_infrastructure_secrets(self):
        mutations = [
            lambda value: value.update(schema=2),
            lambda value: value.update(node_id='another-node'),
            lambda value: value.update(ssh_user='root'),
            lambda value: value.update(release='main;id'),
            lambda value: value['ports'].update(database=5432),
            lambda value: value['environment'].update(POSTGRES_PASSWORD='must-never-leave-main'),
            lambda value: value['environment'].update(SECRET_KEY='short'),
            lambda value: value['environment'].update(SECRET_KEY='x' * 40 + '\nEVIL=1'),
            lambda value: value['environment'].update(ENCRYPTION_KEY='invalid'),
            lambda value: value['environment'].update(ALLOWED_HOSTS='*'),
        ]
        for mutation in mutations:
            value = copy.deepcopy(self.manifest)
            mutation(value)
            with self.subTest(value=list(value)), self.assertRaises(ValueError):
                connection.validate_manifest(value, 'north', ['fiscal'])

    def test_manifest_cannot_redirect_service_connection_outside_the_tunnel(self):
        values = [
            ('DATABASE_URL', 'postgresql://fireisp:fixture@8.8.8.8:15432/fireisp'),
            ('DATABASE_URL', 'postgresql://postgres:fixture@127.0.0.1:15432/fireisp'),
            ('DATABASE_URL', 'postgresql://fireisp:fixture@127.0.0.1:15432/fireisp?host=8.8.8.8'),
            ('DATABASE_URL', 'postgresql://fireisp:fixture@127.0.0.1:15432/fireisp?hostaddr=8.8.8.8'),
            ('REDIS_URL', 'redis://127.0.0.1:16379/0?host=8.8.8.8'),
            ('REDIS_URL', 'redis://127.0.0.1:16379/0?port=9000'),
            ('REDIS_URL', 'redis://127.0.0.1:16379/not-a-database'),
        ]
        for name, url in values:
            value = copy.deepcopy(self.manifest)
            value['environment'][name] = url
            with self.subTest(name=name, url=url), self.assertRaises(ValueError):
                connection.validate_manifest(value, 'north', ['fiscal'])

    def test_forwarding_preserves_encoded_credentials_and_network_identity(self):
        value = copy.deepcopy(self.manifest)
        value['environment'].update(NETWORK_RADIUS_TOKEN='n' * 48, NETWORK_PUBLIC_ENDPOINT='8.8.8.8',
                                    NETWORK_RADIUS_URL='http://127.0.0.1:18000/network/radius')
        environment = connection.validate_manifest(value, 'north', ['network'], '8.8.8.8')
        local = connection.forwarded_environment(environment, connection.PREFERRED_PORTS, ['network'])
        self.assertEqual(local['DATABASE_URL'],
                         'postgresql://fireisp:percent%25colon%3Aat%40@127.0.0.1:25432/fireisp')
        self.assertEqual(local['REDIS_URL'], 'redis://:redis%40fixture@127.0.0.1:26379/0')
        self.assertEqual(local['NETWORK_RADIUS_URL'], 'http://127.0.0.1:28000/network/radius')
        with self.assertRaises(ValueError):
            connection.validate_manifest(value, 'north', ['network'], '1.1.1.1')
        with self.assertRaises(ValueError):
            connection.validate_manifest(value, 'north', ['fiscal'])

    def test_operator_authentication_uses_open_ssh_tty_and_payload_only_on_stdin(self):
        response = Mock(stdout=json.dumps(self.manifest))
        payload = {'public_key': 'ssh-ed25519 synthetic-public-key', 'node_id': 'north', 'roles': ['fiscal']}
        with patch.object(connection.subprocess, 'run', return_value=response) as run:
            self.assertEqual(connection.request_enrollment('main.test', 2222, 'operator', None,
                                                          self.directory / 'known_hosts', payload), self.manifest)
        command = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=ask', command)
        self.assertIn('ClearAllForwardings=yes', command)
        self.assertEqual(command[-1], 'sudo -n python3 /opt/fireisp/app/deploy/pairing.py prepare')
        self.assertNotIn('BatchMode=yes', command)
        self.assertNotIn('stderr', run.call_args.kwargs)
        self.assertNotIn(payload['public_key'], ' '.join(command))
        self.assertEqual(json.loads(run.call_args.kwargs['input']), payload)

    def test_noninteractive_enrollment_requires_pinned_host_and_key_authentication(self):
        with patch.object(connection.subprocess, 'run', return_value=Mock(stdout=json.dumps(self.manifest))) as run:
            connection.request_enrollment('main.test', 22, 'root', None, self.directory / 'known_hosts', {},
                                          interactive=False)
        command = run.call_args.args[0]
        self.assertIn('StrictHostKeyChecking=yes', command)
        self.assertIn('BatchMode=yes', command)
        self.assertIn('GlobalKnownHostsFile=/dev/null', command)
        self.assertEqual(command[1:3], ['-F', '/dev/null'])
        self.assertNotIn('StrictHostKeyChecking=accept-new', command)

    def test_tunnel_service_uses_only_loopback_and_fail_closed_pinned_identity(self):
        unit = connection.tunnel_unit('main.test', 2222, 'north', self.directory, connection.PREFERRED_PORTS)
        for expected in ('StrictHostKeyChecking=yes', 'BatchMode=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none',
                         'ExitOnForwardFailure=yes', 'ClearAllForwardings=no', 'Restart=always',
                         'NoNewPrivileges=yes', 'ProtectSystem=strict', 'fireisp-link@main.test',
                         '127.0.0.1:25432:127.0.0.1:15432', '127.0.0.1:26379:127.0.0.1:16379',
                         '127.0.0.1:28000:127.0.0.1:18000'):
            self.assertIn(expected, unit)
        self.assertNotIn('0.0.0.0', unit)
        self.assertNotIn('StrictHostKeyChecking=no', unit)
        self.assertNotIn('ClearAllForwardings=yes', unit)
        self.assertEqual(connection.systemd_quote('path with %n/$HOME'), '"path with %%n/$$HOME"')

    def test_private_state_rejects_foreign_connection_and_symlink(self):
        path = self.directory / 'connection.json'
        identity = {'main_host': 'main.test', 'ssh_port': 22, 'node_id': 'north'}
        state = {'schema': 1, 'identity': identity, 'local_ports': dict(connection.PREFERRED_PORTS)}
        connection.write_private(path, json.dumps(state))
        self.assertEqual(connection.read_state(path, identity), state)
        with self.assertRaises(ValueError):
            connection.read_state(path, identity | {'main_host': 'foreign.test'})
        link = self.directory / 'linked.json'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            connection.read_state(link, identity)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            connection.read_state(path, identity)

    def test_port_allocation_avoids_existing_listeners_and_keeps_ports_distinct(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(('127.0.0.1', 0))
            occupied = listener.getsockname()[1]
            with patch.object(connection, 'PREFERRED_PORTS', dict.fromkeys(connection.MAIN_PORTS, occupied)):
                ports = connection.reserve_ports()
            self.assertTrue(connection.valid_ports(ports))
            self.assertNotIn(occupied, ports.values())

    def test_link_key_is_reused_without_rotation(self):
        key, public = connection.ensure_link_key(self.directory)
        original = key.read_bytes()
        second_key, second_public = connection.ensure_link_key(self.directory)
        self.assertEqual((second_key, second_public), (key, public))
        self.assertEqual(key.read_bytes(), original)
        self.assertEqual(key.stat().st_mode & 0o777, 0o600)

    def test_install_and_rerun_preserve_transport_and_keep_secrets_out_of_result(self):
        systemd = self.directory / 'systemd'
        systemd.mkdir()
        response = copy.deepcopy(self.manifest)

        def enroll(host, port, user, key, known_hosts, payload, **kwargs):
            connection.write_private(known_hosts, 'main.test ssh-ed25519 synthetic-pinned-key\n')
            return response

        options = {'directory': self.directory / 'connections', 'systemd_directory': systemd}
        with patch.object(connection, 'require_root'), patch.object(connection, 'request_enrollment', side_effect=enroll), \
                patch.object(connection, 'wait_for_tunnel'), patch.object(connection.subprocess, 'run') as run:
            # Generate an actual key before mocking subprocess for systemd.
            with patch.object(connection, 'ensure_link_key', return_value=(Path('synthetic-key'), 'ssh-ed25519 fixture')):
                first = connection.connect_main('main.test', 22, 'root', None, 'north', ['fiscal'], **options)
                run.reset_mock()
                second = connection.connect_main('main.test', 22, 'root', None, 'north', ['billing', 'fiscal'], **options)
        self.assertEqual(first['ports'], second['ports'])
        self.assertEqual(first['environment_file'], second['environment_file'])
        self.assertEqual(second['roles'], ['billing', 'fiscal'])
        self.assertEqual([call.args[0] for call in run.call_args_list],
                         [['systemctl', 'enable', '--now', 'fireisp-link-north.service']])
        actual = load_environment(second['environment_file'])
        self.assertEqual(actual['SECRET_KEY'], self.manifest['environment']['SECRET_KEY'])
        self.assertNotIn('environment', second)
        self.assertNotIn(self.manifest['environment']['SECRET_KEY'], repr(second))
        for path in (second['environment_file'], second['directory'] / 'connection.json',
                     second['directory'] / 'known_hosts'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unpinned_response_never_starts_tunnel_or_writes_environment(self):
        systemd = self.directory / 'systemd'
        systemd.mkdir()
        with patch.object(connection, 'require_root'), patch.object(connection, 'ensure_link_key',
                return_value=(Path('synthetic-key'), 'ssh-ed25519 fixture')), \
                patch.object(connection, 'request_enrollment', return_value=self.manifest), \
                patch.object(connection.subprocess, 'run') as run:
            with self.assertRaisesMessage(ValueError, 'host key'):
                connection.connect_main('main.test', 22, 'root', None, 'north', ['fiscal'],
                                        directory=self.directory / 'connections', systemd_directory=systemd)
        run.assert_not_called()
        self.assertFalse((self.directory / 'connections/north/node.env').exists())

    def test_ssh_failure_does_not_echo_json_or_command_error_details(self):
        with patch.object(connection, 'require_root'), patch.object(connection, 'ensure_link_key',
                return_value=(Path('synthetic-key'), 'ssh-ed25519 fixture')), \
                patch.object(connection, 'request_enrollment', side_effect=subprocess.CalledProcessError(
                    255, ['ssh'], output='untrusted-secret-output')):
            with self.assertRaises(RuntimeError) as error:
                connection.connect_main('main.test', 22, 'root', None, 'north', ['fiscal'],
                                        directory=self.directory / 'connections')
        self.assertNotIn('untrusted-secret-output', str(error.exception))

    def test_wait_has_a_deadline_and_does_not_claim_a_broken_tunnel_is_ready(self):
        with patch.object(connection.time, 'monotonic', side_effect=[0, 0, 31, 31]), \
                patch.object(connection.time, 'sleep'), \
                patch.object(connection.socket, 'create_connection', side_effect=OSError('unreachable')):
            with self.assertRaisesMessage(RuntimeError, 'did not start'):
                connection.wait_for_tunnel(connection.PREFERRED_PORTS, timeout=30)
