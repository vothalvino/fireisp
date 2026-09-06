import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import tempfile
import termios
import time

from django.test import SimpleTestCase


BOOTSTRAP = Path(__file__).resolve().parents[1] / 'install.sh'


class BootstrapTests(SimpleTestCase):
    """Exercise the downloaded bootstrap without installing packages or using root."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.cleanup_directory)
        # Load the real functions without invoking the machine-changing entry point.
        source = BOOTSTRAP.read_text()
        self.assertTrue(source.endswith('bootstrap_main "$@"\n'))
        self.functions = source.removesuffix('bootstrap_main "$@"\n')

    def cleanup_directory(self):
        for directory, children, files in os.walk(self.directory):
            Path(directory).chmod(0o700)
            for name in children:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o700)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o600)
        self.temporary.cleanup()

    def shell(self, code, *arguments):
        return subprocess.run(['bash', '-c', self.functions + '\n' + code, 'bootstrap-test', *arguments],
                              capture_output=True, text=True, timeout=20)

    def repository(self):
        repository = self.directory / 'origin'
        repository.mkdir()
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(repository)], check=True)
        wizard = repository / 'deploy' / 'wizard.py'
        wizard.parent.mkdir()
        wizard.write_text('print("fixture wizard")\n')
        first = self.commit(repository)
        (repository / 'second.txt').write_text('new main release\n')
        second = self.commit(repository)
        installer = self.directory / 'installer'
        (installer / 'releases').mkdir(parents=True)
        return repository, installer, first, second

    def commit(self, repository):
        subprocess.run(['git', '-C', str(repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repository), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.test', 'commit', '-q', '-m', 'fixture'], check=True)
        return subprocess.check_output(['git', '-C', str(repository), 'rev-parse', 'HEAD'], text=True).strip()

    def test_help_and_invalid_release_do_not_need_root_or_network(self):
        help_result = subprocess.run(['bash', str(BOOTSTRAP), '--help'], capture_output=True, text=True, timeout=5)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn('sudo bash', help_result.stdout)
        for arguments in (['--release'], ['--release='], ['--release', 'main'], ['--release', '../outside']):
            with self.subTest(arguments=arguments):
                result = subprocess.run(['bash', str(BOOTSTRAP), *arguments],
                                        capture_output=True, text=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Git commit', result.stderr)

    def test_argument_forwarding_preserves_metacharacters_without_evaluation(self):
        arguments = ['--release', 'A' * 40, '--node-id', 'fiscal-a', '--', '--help',
                     'literal $(touch forbidden) `id` $HOME; newline\nvalue']
        result = self.shell('bootstrap_parse_arguments "$@"\nprintf "%s\\0" "$FIREISP_SELECTED_RELEASE" '
                            '"${FIREISP_WIZARD_ARGUMENTS[@]}"', *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split('\0')[:-1], ['a' * 40, *arguments[2:4], *arguments[5:]])

    def test_main_is_resolved_once_then_exact_commit_is_fetched(self):
        repository, installer, _, latest = self.repository()
        result = self.shell('FIREISP_REPOSITORY="$1"\nFIREISP_INSTALLER_ROOT="$2"\n'
                            'bootstrap_resolve_release\nbootstrap_checkout "$FIREISP_SELECTED_RELEASE"\n'
                            'printf "%s" "$FIREISP_SELECTED_RELEASE"', str(repository), str(installer))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, latest)
        checkout = installer / 'releases' / latest
        self.assertEqual((checkout / 'second.txt').read_text(), 'new main release\n')
        self.assertEqual(checkout.stat().st_mode & 0o222, 0)
        self.assertEqual((checkout / 'deploy/wizard.py').stat().st_mode & 0o222, 0)
        self.assertFalse((self.directory / 'app').exists())

    def test_explicit_release_is_not_replaced_with_current_main(self):
        repository, installer, first, latest = self.repository()
        result = self.shell('FIREISP_REPOSITORY="$1"\nFIREISP_INSTALLER_ROOT="$2"\n'
                            'FIREISP_SELECTED_RELEASE="$3"\nbootstrap_resolve_release\n'
                            'bootstrap_checkout "$FIREISP_SELECTED_RELEASE"',
                            str(repository), str(installer), first)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((installer / 'releases' / first / 'deploy/wizard.py').exists())
        self.assertFalse((installer / 'releases' / first / 'second.txt').exists())
        self.assertFalse((installer / 'releases' / latest).exists())

    def test_wrong_commit_modified_files_and_ignored_files_fail_verification(self):
        repository, _, first, latest = self.repository()
        wrong = self.shell('bootstrap_verify_checkout "$1" "$2"', str(repository), first)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn('does not match', wrong.stderr)
        (repository / 'deploy/wizard.py').write_text('print("modified")\n')
        modified = self.shell('bootstrap_verify_checkout "$1" "$2"', str(repository), latest)
        self.assertNotEqual(modified.returncode, 0)
        self.assertIn('was modified', modified.stderr)
        subprocess.run(['git', '-C', str(repository), 'restore', 'deploy/wizard.py'], check=True)
        (repository / '.git/info/exclude').write_text('ignored-file\n')
        (repository / 'ignored-file').write_text('unexpected ignored file\n')
        ignored = self.shell('bootstrap_verify_checkout "$1" "$2"', str(repository), latest)
        self.assertNotEqual(ignored.returncode, 0)
        self.assertIn('was modified', ignored.stderr)

    def test_interrupted_download_is_removed_and_never_published(self):
        repository, installer, first, _ = self.repository()
        result = self.shell('FIREISP_REPOSITORY="$1"\nFIREISP_INSTALLER_ROOT="$2"\n'
                            'git() { if [[ "$*" == *fetch* ]]; then return 1; fi; command git "$@"; }\n'
                            'sleep() { :; }\ntrap bootstrap_cleanup EXIT\nbootstrap_checkout "$3"',
                            str(repository), str(installer), first)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No installer was executed', result.stderr)
        self.assertEqual(list((installer / 'releases').iterdir()), [])

    def test_symbolic_link_directory_and_writable_parent_are_rejected(self):
        link = self.directory / 'link'
        link.symlink_to(self.directory, target_is_directory=True)
        result = self.shell('bootstrap_secure_directory "$1"', str(link))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symbolic-link directory', result.stderr)
        result = self.shell('bootstrap_secure_directory /tmp')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not writable by other users', result.stderr)

    def test_piped_script_reopens_controlling_terminal_for_wizard_and_preserves_arguments(self):
        installer = self.directory / 'installer'
        release = 'a' * 40
        wizard = installer / 'releases' / release / 'deploy/wizard.py'
        wizard.parent.mkdir(parents=True)
        wizard.write_text('import json, sys\nreply = input("Select module: ")\n'
                          'print("RESULT=" + json.dumps({"reply": reply, "args": sys.argv[1:]}))\n')
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)

        def controlling_terminal():
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        arguments = ['--node-id', 'literal $value; unchanged']
        code = (self.functions + f'\nFIREISP_INSTALLER_ROOT={shlex.quote(str(installer))}\n'
                f'FIREISP_SELECTED_RELEASE={release}\n'
                f'FIREISP_WIZARD_ARGUMENTS=({shlex.join(arguments)})\nbootstrap_launch_wizard\n')
        process = subprocess.Popen(['bash'], stdin=subprocess.PIPE, stdout=slave, stderr=slave,
                                   pass_fds=(slave,), preexec_fn=controlling_terminal)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        process.stdin.write(code.encode())
        process.stdin.close()
        output = b''
        answered = False
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                output += os.read(master, 8192)
            if b'Select module: ' in output and not answered:
                os.write(master, b'fiscal\n')
                answered = True
            if b'RESULT=' in output and process.poll() is not None:
                break
        self.assertTrue(answered, output.decode())
        self.assertEqual(process.wait(timeout=2), 0, output.decode())
        result = next(line.split('RESULT=', 1)[1] for line in output.decode().splitlines() if 'RESULT=' in line)
        self.assertEqual(json.loads(result), {'reply': 'fiscal', 'args': arguments})
