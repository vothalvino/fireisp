import base64
import io
import json
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from deploy.nodes import install


class NodeDeploymentTests(SimpleTestCase):
    release = 'a' * 40

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.values = {
            'SECRET_KEY': 'synthetic-dollar$and-long-application-secret-for-tests',
            'ENCRYPTION_KEY': base64.urlsafe_b64encode(b'x' * 32).decode(),
            'DATABASE_URL': 'postgresql://fireisp:synthetic@127.0.0.1:15432/fireisp',
            'REDIS_URL': 'redis://127.0.0.1:16379/0',
            'ALLOWED_HOSTS': 'isp.example.test,localhost',
            'CSRF_TRUSTED_ORIGINS': 'https://isp.example.test',
            'POSTGRES_PASSWORD': 'database-administrator-secret-must-not-leave-control-server',
            'POSTGRES_USER': 'postgres',
            'FIREISP_DB_PASSWORD': 'duplicate-secret-must-not-be-forwarded',
            'DEBUG': 'true',
            'NETWORK_RADIUS_TOKEN': 'synthetic-radius-token-' + 'x' * 40,
            'NETWORK_RADIUS_URL': 'http://127.0.0.1:18000/network/radius',
            'NETWORK_PUBLIC_ENDPOINT': '8.8.8.8',
        }

    def environment(self, role='worker', **changes):
        return install.runtime_environment({**self.values, **changes}, role, 'node-a', self.release, True)

    def write_environment(self):
        path = self.directory / 'private.env'
        path.write_text(''.join(f'{key}={value}\n' for key, value in self.values.items()))
        path.chmod(0o600)
        return path

    def test_nonnetwork_roles_never_receive_infrastructure_or_router_credentials(self):
        for role in ('web', 'worker', 'billing', 'fiscal', 'scheduler'):
            with self.subTest(role=role):
                environment = self.environment(role)
                for name in ('POSTGRES_PASSWORD', 'POSTGRES_USER', 'FIREISP_DB_PASSWORD',
                             'NETWORK_RADIUS_TOKEN', 'NETWORK_PUBLIC_ENDPOINT'):
                    self.assertNotIn(name, environment)
                self.assertEqual(environment['DEBUG'], 'false')
                self.assertEqual(environment['FIREISP_RELEASE'], self.release)

    def test_public_databases_and_brokers_and_implicit_loopback_are_rejected(self):
        for kind, value in [('DATABASE_URL', 'postgresql://fireisp:secret@8.8.8.8/fireisp'),
                            ('REDIS_URL', 'redis://8.8.8.8/0'),
                            ('DATABASE_URL', self.values['DATABASE_URL']),
                            ('REDIS_URL', self.values['REDIS_URL'])]:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                install.validate_url(value, kind)

    def test_private_dns_must_not_also_resolve_to_a_public_address(self):
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.42.0.10', 5432))
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 5432))
        with patch.object(install.socket, 'getaddrinfo', return_value=[private]):
            install.validate_url('postgresql://fireisp:synthetic@db.internal/fireisp', 'DATABASE_URL')
        with patch.object(install.socket, 'getaddrinfo', return_value=[private, public]):
            with self.assertRaises(ValueError):
                install.validate_url('postgresql://fireisp:synthetic@db.internal/fireisp', 'DATABASE_URL')

    def test_url_query_cannot_bypass_private_endpoint_or_disable_redis_tls(self):
        invalid = [
            ('DATABASE_URL', self.values['DATABASE_URL'] + '?host=8.8.8.8'),
            ('DATABASE_URL', self.values['DATABASE_URL'] + '?hostaddr=8.8.8.8'),
            ('DATABASE_URL', 'postgresql://postgres:synthetic@127.0.0.1/fireisp'),
            ('REDIS_URL', 'redis://127.0.0.1/0?host=8.8.8.8'),
            ('REDIS_URL', 'rediss://127.0.0.1/0?ssl_cert_reqs=none'),
            ('REDIS_URL', 'rediss://127.0.0.1/0?ssl_check_hostname=false'),
            ('NETWORK_RADIUS_URL', 'http://127.0.0.1:18000/login/'),
            ('NETWORK_RADIUS_URL', 'http://user:secret@127.0.0.1:18000/network/radius'),
        ]
        for kind, value in invalid:
            with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                install.validate_url(value, kind, True)

    def test_network_role_requires_node_callback_identity_and_own_endpoint(self):
        environment = self.environment('network')
        self.assertEqual(environment['NETWORK_NODE_ID'], 'node-a')
        self.assertEqual(environment['NETWORK_HEALTH_URL'], 'http://127.0.0.1:18000/healthz')
        for changes in ({'NETWORK_RADIUS_TOKEN': ''}, {'NETWORK_RADIUS_URL': ''},
                        {'NETWORK_PUBLIC_ENDPOINT': '127.0.0.1'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.environment('network', **changes)

    def test_role_compose_does_not_publish_ports_or_provision_shared_databases(self):
        for role in install.ROLES:
            with self.subTest(role=role):
                compose = install.build_compose(self.environment(role), role, 'node-a', 'fireisp:fixture',
                                                agent_image='agent:fixture', radius_image='radius:fixture',
                                                radius_url=self.values['NETWORK_RADIUS_URL'])
                for service in compose['services'].values():
                    self.assertNotIn('ports', service)
                    self.assertNotIn('privileged', service)
                self.assertNotIn('db', compose['services'])
                self.assertNotIn('redis', compose['services'])
                self.assertNotIn('caddy', compose['services'])
                application = compose['services'][role]
                self.assertEqual(application['cap_drop'], ['ALL'])
                self.assertTrue(application['read_only'])
                if role == 'web':
                    self.assertIn('127.0.0.1:18000', application['command'])
                if role == 'network':
                    radius_environment = compose['services']['radius']['environment']
                    self.assertNotIn('DATABASE_URL', radius_environment)
                    self.assertNotIn('ENCRYPTION_KEY', radius_environment)
                    self.assertEqual(radius_environment['NETWORK_NODE_ID'], 'node-a')

    def test_worker_roles_use_guarded_role_command_and_scheduler_uses_singleton_command(self):
        for role in ('worker', 'billing', 'fiscal'):
            compose = install.build_compose(self.environment(role), role, 'node-a', 'fireisp:fixture', concurrency=3)
            self.assertEqual(compose['services'][role]['command'],
                             ['python', 'manage.py', 'run_role', '--role', role, '--concurrency', '3'])
        compose = install.build_compose(self.environment('scheduler'), 'scheduler', 'node-a', 'fireisp:fixture')
        self.assertEqual(compose['services']['scheduler']['command'], ['python', 'manage.py', 'run_scheduler'])

    def test_compose_escapes_literal_dollars_in_secrets(self):
        compose = install.build_compose(self.environment(), 'worker', 'node-a', 'fireisp:fixture')
        self.assertEqual(compose['services']['worker']['environment']['SECRET_KEY'],
                         self.values['SECRET_KEY'].replace('$', '$$'))

    def test_environment_parser_does_not_expand_shell_text_and_enforces_private_file(self):
        path = self.write_environment()
        with path.open('a') as stream:
            stream.write('EXTRA_SECRET=\'$(touch /tmp/fireisp-must-not-run) $HOME `id`\'\n')
        values = install.load_environment(path)
        self.assertEqual(values['EXTRA_SECRET'], '$(touch /tmp/fireisp-must-not-run) $HOME `id`')
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            install.load_environment(path)
        path.chmod(0o600)
        link = self.directory / 'link.env'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            install.load_environment(link)

    def test_check_only_redacts_every_secret_and_does_not_launch_processes(self):
        path = self.write_environment()
        output = io.StringIO()
        arguments = ['install.py', '--role', 'fiscal', '--node-id', 'fiscal-a', '--env-file', str(path),
                     '--release', self.release, '--allow-loopback-tunnels', '--check-only']
        with patch.object(install.sys, 'argv', arguments), patch('sys.stdout', output), patch.object(install, 'run') as run:
            install.main()
        run.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertTrue(result['valid'])
        self.assertEqual(result['services'], ['fiscal'])
        for key in ('DATABASE_URL', 'SECRET_KEY', 'ENCRYPTION_KEY', 'POSTGRES_PASSWORD', 'NETWORK_RADIUS_TOKEN'):
            self.assertNotIn(self.values[key], output.getvalue())
        self.assertFalse((self.directory / 'compose.json').exists())

    def test_image_revision_mismatch_fails_before_launch(self):
        with patch.object(install.subprocess, 'check_output', return_value='b' * 40):
            with self.assertRaises(ValueError):
                install.verify_image('fireisp:fixture', self.release)

    def test_private_configuration_write_rejects_links_and_restricts_permissions(self):
        path = self.directory / 'compose.json'
        install.write_private(path, '{}')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        link = self.directory / 'config-link.json'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            install.write_private(link, 'must not overwrite target')
        self.assertEqual(path.read_text(), '{}')

    def test_required_keys_node_identifier_and_revision_fail_closed(self):
        for values, node_id, release in [
            ({**self.values, 'SECRET_KEY': ''}, 'node-a', self.release),
            ({**self.values, 'ENCRYPTION_KEY': 'bad'}, 'node-a', self.release),
            ({**self.values, 'ALLOWED_HOSTS': '*'}, 'node-a', self.release),
            (self.values, '../node', self.release), (self.values, 'node-a', 'latest'),
        ]:
            with self.subTest(node_id=node_id, release=release), self.assertRaises(ValueError):
                install.runtime_environment(values, 'worker', node_id, release, True)
