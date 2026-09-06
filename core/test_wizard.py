import argparse
from contextlib import nullcontext
import io
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch

from django.test import SimpleTestCase

from deploy import wizard


class WizardTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.environment = self.directory / 'main.env'
        self.environment_patch = patch.object(wizard, 'MAIN_ENV', self.environment)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.output = io.StringIO()
        self.output_patch = patch('sys.stdout', self.output)
        self.output_patch.start()
        self.addCleanup(self.output_patch.stop)

    def options(self, **changes):
        values = dict(mode=None, modules=None, hostname=None, public_ip=None, node_id=None,
                      main_host=None, ssh_port=None, admin_user=None, admin_key=None,
                      network_endpoint=None, no_input=False)
        return argparse.Namespace(**{**values, **changes})

    def main_environment(self, profiles):
        self.environment.write_text("STAGING_HOSTNAME='isp.example.test'\nNETWORK_PUBLIC_ENDPOINT='8.8.8.8'\n"
                                    f"COMPOSE_PROFILES='{profiles}'\n")
        self.environment.chmod(0o600)

    def test_main_and_additional_selection_have_distinct_allowed_roles(self):
        self.assertEqual(wizard.parse_modules('3, billing,2,billing', 'main'), ['billing', 'fiscal', 'network'])
        self.assertEqual(wizard.parse_modules('none', 'main'), [])
        self.assertEqual(wizard.parse_modules('0', 'main'), [])
        self.assertEqual(wizard.parse_modules('all', 'additional'), [role for role, _ in wizard.MODULES])
        self.assertEqual(wizard.parse_modules('6,4', 'additional'), ['worker', 'web'])
        for value, mode in [('scheduler', 'main'), ('6', 'main'), ('none', 'additional'),
                            ('0', 'additional'), ('fiscal,', 'additional'), ('$(id)', 'main')]:
            with self.subTest(value=value, mode=mode), self.assertRaises(ValueError):
                wizard.parse_modules(value, mode)

    def test_existing_main_placement_is_the_interactive_default_including_none(self):
        for profiles, expected in [('billing,network', ['billing', 'network']), ('', [])]:
            self.main_environment(profiles)
            with self.subTest(profiles=profiles), patch('builtins.input', side_effect=['', '', '', '']):
                plan = wizard.collect_options(self.options(), {})
            self.assertEqual(plan, {'mode': 'main', 'roles': expected,
                                    'hostname': 'isp.example.test', 'public_ip': '8.8.8.8'})

    def test_main_server_cannot_be_reinitialized_as_an_additional_server(self):
        self.main_environment('billing')
        with self.assertRaisesRegex(ValueError, 'already a main server'):
            wizard.collect_options(self.options(mode='additional', modules='fiscal', no_input=True), {})
        with self.assertRaisesRegex(ValueError, 'different installation mode'):
            wizard.collect_options(self.options(mode='main', modules='billing', no_input=True),
                                   {'mode': 'additional'})

    def test_noninteractive_main_requires_selection_and_validates_connection_values(self):
        with self.assertRaisesRegex(ValueError, '--modules'):
            wizard.collect_options(self.options(mode='main', no_input=True), {})
        plan = wizard.collect_options(self.options(mode='main', modules='fiscal', hostname='ISP.Example.Test',
                                                   public_ip='8.8.8.8', no_input=True), {})
        self.assertEqual(plan['hostname'], 'isp.example.test')
        self.assertEqual(plan['roles'], ['fiscal'])
        for validator, invalid in [(wizard.validate_hostname, 'https://isp.example.test'),
                                   (wizard.validate_public_ip, '127.0.0.1'),
                                   (wizard.validate_node_id, 'primary'),
                                   (wizard.validate_node_id, '../../main'),
                                   (wizard.valid_port, '65536'), (wizard.valid_user, 'root;id')]:
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validator(invalid)

    def test_additional_options_collect_connection_and_only_network_requires_public_endpoint(self):
        options = self.options(mode='additional', modules='billing,fiscal', node_id='remote-1',
                               main_host='isp.example.test', no_input=True)
        plan = wizard.collect_options(options, {})
        self.assertEqual(plan['roles'], ['billing', 'fiscal'])
        self.assertEqual(plan['ssh_port'], 22)
        self.assertEqual(plan['admin_user'], 'root')
        self.assertNotIn('network_endpoint', plan)
        options.modules = 'network'
        with self.assertRaisesRegex(ValueError, '--network-endpoint'):
            wizard.collect_options(options, {})
        options.network_endpoint = '8.8.4.4'
        self.assertEqual(wizard.collect_options(options, {})['network_endpoint'], '8.8.4.4')

    def test_existing_additional_identity_cannot_be_changed_and_orphan_its_workers(self):
        options = self.options(mode='additional', modules='fiscal', node_id='replacement-id',
                               main_host='isp.example.test', no_input=True)
        with self.assertRaisesRegex(ValueError, 'existing server name'):
            wizard.collect_options(options, {'mode': 'additional', 'node_id': 'original-id', 'roles': ['billing']})

    def test_saved_selection_is_private_and_rejects_untrusted_files(self):
        path = self.directory / 'wizard.json'
        state = {'mode': 'additional', 'node_id': 'remote-1', 'roles': ['fiscal']}
        wizard.save_state(state, path)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(wizard.read_state(path), state)
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            wizard.read_state(path)
        path.chmod(0o600)
        link = self.directory / 'linked.json'
        link.symlink_to(path)
        with self.assertRaises(ValueError):
            wizard.read_state(link)
        missing_link = self.directory / 'dangling.json'
        missing_link.symlink_to(self.directory / 'missing.json')
        with self.assertRaises(ValueError):
            wizard.read_state(missing_link)

    def test_failed_state_replacement_preserves_the_previous_selection(self):
        path = self.directory / 'wizard.json'
        original = {'mode': 'main', 'roles': ['billing']}
        wizard.save_state(original, path)
        with patch('deploy.connection.os.replace', side_effect=OSError('Interrupted write')):
            with self.assertRaises(OSError):
                wizard.save_state({'mode': 'main', 'roles': ['fiscal']}, path)
        self.assertEqual(wizard.read_state(path), original)

    def test_additional_installs_main_release_and_stops_only_removed_owned_roles_after_health(self):
        plan = {'mode': 'additional', 'node_id': 'remote-1', 'roles': ['billing', 'fiscal'],
                'main_host': 'isp.example.test', 'ssh_port': 22, 'admin_user': 'root', 'admin_key': ''}
        previous = {**plan, 'roles': ['billing', 'network']}
        connection = {'release': 'a' * 40, 'environment_file': self.directory / 'private.env'}
        source = self.directory / 'latest'
        matching = self.directory / 'main-release'
        events = []
        with patch('deploy.install.ensure_docker'), patch('deploy.connection.connect_main', return_value=connection) as connect, \
                patch.object(wizard, 'matching_source', return_value=matching) as match, \
                patch.object(wizard.shutil, 'which', return_value='/usr/bin/ssh'), \
                patch.object(wizard.subprocess, 'run', side_effect=lambda command, **kwargs: events.append(('run', command))), \
                patch.object(wizard, 'wait_for_role', side_effect=lambda directory, role, node_id: events.append(('healthy', role))), \
                patch.object(Path, 'exists', return_value=True):
            wizard.install_additional(plan, previous, source)
        connect.assert_called_once()
        match.assert_called_once_with(source, connection['release'])
        self.assertEqual([kind for kind, _ in events], ['run', 'healthy', 'run', 'healthy', 'run'])
        for index, role in [(0, 'billing'), (2, 'fiscal')]:
            command = events[index][1]
            self.assertEqual(command[1], str(matching / 'deploy/nodes/install.py'))
            self.assertEqual(command[command.index('--role') + 1], role)
            self.assertEqual(command[command.index('--node-id') + 1], 'remote-1-' + role)
            self.assertEqual(command[command.index('--release') + 1], connection['release'])
            self.assertEqual(command[command.index('--env-file') + 1], str(connection['environment_file']))
            self.assertIn('--allow-loopback-tunnels', command)
        stopped = events[-1][1]
        self.assertIn('/opt/fireisp/nodes/remote-1-network/compose.json', stopped)
        self.assertEqual(stopped[-3:], ['stop', '--timeout', '180'])
        for _, command in [event for event in events if event[0] == 'run']:
            self.assertFalse(any(value.endswith('/deploy/install.py') for value in command))
            self.assertNotIn('migrate', command)
            self.assertNotIn('bootstrap', command)

    def test_failed_new_role_keeps_previous_roles_running(self):
        plan = {'mode': 'additional', 'node_id': 'remote-1', 'roles': ['fiscal'],
                'main_host': 'isp.example.test', 'ssh_port': 22, 'admin_user': 'root', 'admin_key': ''}
        with patch('deploy.install.ensure_docker'), \
                patch('deploy.connection.connect_main', return_value={'release': 'a' * 40, 'environment_file': '/private.env'}), \
                patch.object(wizard, 'matching_source', return_value=self.directory), \
                patch.object(wizard.shutil, 'which', return_value='/usr/bin/ssh'), \
                patch.object(wizard.subprocess, 'run') as run, \
                patch.object(wizard, 'wait_for_role', side_effect=RuntimeError('No heartbeat')), \
                patch.object(Path, 'exists', return_value=True):
            with self.assertRaisesRegex(RuntimeError, 'No heartbeat'):
                wizard.install_additional(plan, {**plan, 'roles': ['network']}, self.directory)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('stop', run.call_args.args[0])

    def test_unrelated_previous_node_roles_are_never_stopped(self):
        plan = {'mode': 'additional', 'node_id': 'remote-2', 'roles': ['fiscal'],
                'main_host': 'isp.example.test', 'ssh_port': 22, 'admin_user': 'root', 'admin_key': ''}
        with patch('deploy.install.ensure_docker'), \
                patch('deploy.connection.connect_main', return_value={'release': 'a' * 40, 'environment_file': '/private.env'}), \
                patch.object(wizard, 'matching_source', return_value=self.directory), \
                patch.object(wizard.shutil, 'which', return_value='/usr/bin/ssh'), \
                patch.object(wizard.subprocess, 'run') as run, patch.object(wizard, 'wait_for_role'), \
                patch.object(Path, 'exists', return_value=True):
            wizard.install_additional(plan, {'node_id': 'remote-1', 'roles': ['network']}, self.directory)
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('stop', run.call_args.args[0])

    def test_cancel_before_install_preserves_saved_selection_without_installing(self):
        errors = io.StringIO()
        with patch.object(wizard.sys, 'argv', ['wizard.py']), patch.object(wizard.os, 'geteuid', return_value=0), \
                patch('deploy.install.supported_ubuntu', return_value=True), \
                patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.object(wizard, 'installer_lock', return_value=nullcontext()), \
                patch.object(wizard, 'read_state', return_value={'mode': 'main'}), \
                patch.object(wizard, 'collect_options', side_effect=KeyboardInterrupt), \
                patch.object(wizard, 'save_state') as save, patch.object(wizard, 'install_main') as main_install, \
                patch.object(wizard, 'install_additional') as additional_install, patch('sys.stderr', errors):
            with self.assertRaises(SystemExit) as stopped:
                wizard.main()
        self.assertEqual(stopped.exception.code, 130)
        self.assertIn('interrupted', errors.getvalue())
        save.assert_not_called()
        main_install.assert_not_called()
        additional_install.assert_not_called()

    def test_successful_install_is_required_before_saving_selected_modules(self):
        plan = {'mode': 'main', 'roles': ['billing'], 'hostname': 'isp.example.test', 'public_ip': '8.8.8.8'}
        with patch.object(wizard.sys, 'argv', ['wizard.py']), patch.object(wizard.os, 'geteuid', return_value=0), \
                patch('deploy.install.supported_ubuntu', return_value=True), \
                patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.object(wizard, 'installer_lock', return_value=nullcontext()), \
                patch.object(wizard, 'read_state', return_value={}), \
                patch.object(wizard, 'collect_options', return_value=plan), \
                patch.object(wizard, 'save_state') as save, \
                patch.object(wizard, 'install_main', side_effect=RuntimeError('Readiness failed')), \
                patch('sys.stderr', io.StringIO()):
            with self.assertRaises(SystemExit):
                wizard.main()
        save.assert_not_called()

    def test_interruption_during_install_does_not_claim_runtime_changes_were_rolled_back(self):
        plan = {'mode': 'main', 'roles': ['billing'], 'hostname': 'isp.example.test', 'public_ip': '8.8.8.8'}
        errors = io.StringIO()
        with patch.object(wizard.sys, 'argv', ['wizard.py']), patch.object(wizard.os, 'geteuid', return_value=0), \
                patch('deploy.install.supported_ubuntu', return_value=True), \
                patch.object(wizard.sys.stdin, 'isatty', return_value=True), \
                patch.object(wizard, 'installer_lock', return_value=nullcontext()), \
                patch.object(wizard, 'read_state', return_value={}), \
                patch.object(wizard, 'collect_options', return_value=plan), \
                patch.object(wizard, 'save_state') as save, \
                patch.object(wizard, 'install_main', side_effect=KeyboardInterrupt), patch('sys.stderr', errors):
            with self.assertRaises(SystemExit) as stopped:
                wizard.main()
        self.assertEqual(stopped.exception.code, 130)
        save.assert_not_called()
        self.assertIn('Some steps may already be installed', errors.getvalue())
        self.assertNotIn('Existing settings were preserved', errors.getvalue())

    def test_matching_source_fetches_the_main_commit_without_following_newer_main(self):
        repository = self.directory / 'repository'
        repository.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repository)], check=True)

        def commit(contents):
            (repository / 'marker').write_text(contents)
            subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
            subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
                            '-c', 'user.email=fixture@example.test', 'commit', '-q', '-m', 'fixture'], check=True)
            return subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()

        older = commit('matching main server')
        newer = commit('newer repository main')
        with patch.object(wizard, 'REPOSITORY', str(repository)):
            matching = wizard.matching_source(repository, older, self.directory / 'releases')
        self.assertEqual(matching.name, older)
        self.assertEqual((matching / 'marker').read_text(), 'matching main server')
        self.assertEqual(matching.stat().st_mode & 0o222, 0)
        self.assertEqual((matching / 'marker').stat().st_mode & 0o222, 0)
        self.assertEqual(subprocess.check_output(['git', '-C', str(matching), 'rev-parse', 'HEAD'], text=True).strip(), older)
        self.assertNotEqual(older, newer)
        with self.assertRaises(ValueError):
            wizard.matching_source(repository, 'main', self.directory / 'releases')

    def test_saved_json_contains_choices_without_password_fields(self):
        plan = {'mode': 'additional', 'node_id': 'remote-1', 'roles': ['fiscal'],
                'main_host': 'isp.example.test', 'ssh_port': 22, 'admin_user': 'root',
                'admin_key': '/root/.ssh/existing-key'}
        path = self.directory / 'wizard.json'
        wizard.save_state(plan, path)
        self.assertEqual(json.loads(path.read_text()), plan)
        self.assertNotIn('password', path.read_text().lower())
