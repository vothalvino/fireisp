from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings

from core.models import DeploymentState, RuntimeNode
from core.runtime import ROLE_QUEUES, heartbeat, scheduler_lock, supervise, validate_release
from deploy.install import local_profiles, release_identity
from fireisp.celery import app


@override_settings(FIREISP_RELEASE='a' * 40, FIREISP_NODE_ID='test-node')
class RuntimeTests(TestCase):
    def setUp(self):
        DeploymentState.objects.create(pk=1, release='a' * 40)

    def test_worker_version_mismatch_fails_before_process_start(self):
        with override_settings(FIREISP_RELEASE='b' * 40), patch('core.runtime.subprocess.Popen') as start:
            with self.assertRaises(RuntimeError):
                supervise(['unused'], 'fiscal')
            start.assert_not_called()
        self.assertFalse(RuntimeNode.objects.exists())

    def test_roles_have_separate_observable_heartbeats(self):
        first = heartbeat('billing')
        second = heartbeat('fiscal')
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(heartbeat('billing').pk, first.pk)
        self.assertEqual(RuntimeNode.objects.count(), 2)
        self.assertEqual(first.release, 'a' * 40)

    def test_missing_cluster_version_is_rejected_outside_development(self):
        DeploymentState.objects.all().delete()
        with override_settings(DEBUG=False, TESTING=False):
            with self.assertRaises(RuntimeError):
                validate_release()

    def test_release_initializer_validates_identity(self):
        with self.assertRaises(CommandError):
            call_command('set_deployment_release', release='latest')
        call_command('set_deployment_release', release='b' * 40, verbosity=0)
        self.assertEqual(DeploymentState.objects.get(pk=1).release, 'b' * 40)

    def test_leadership_loss_terminates_scheduler_child(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        ownership = Mock(side_effect=[True, False])
        with patch('core.runtime.subprocess.Popen', return_value=process), patch('core.runtime.os.killpg') as kill:
            with self.assertRaises(RuntimeError):
                supervise(['unused'], 'scheduler', ownership_check=ownership)
        process.terminate.assert_called_once()
        kill.assert_not_called()
        process.wait.assert_called_once_with(timeout=5)
        self.assertEqual(RuntimeNode.objects.get().status, 'failed')

    def test_lost_leadership_before_spawn_never_starts_scheduler(self):
        with patch('core.runtime.subprocess.Popen') as start:
            with self.assertRaises(RuntimeError):
                supervise(['unused'], 'scheduler', ownership_check=lambda: False)
        start.assert_not_called()


class RoleConfigurationTests(SimpleTestCase):
    def test_heavy_work_is_routed_to_its_own_queue(self):
        for name, expected in [('core.tasks.deliver_outbox', 'core'),
                               ('billing.tasks.renewal_preview', 'billing'),
                               ('fiscal.tasks.process_fiscal_job', 'fiscal')]:
            route = app.amqp.router.route({}, name)
            self.assertEqual(route['queue'].name, expected)
        self.assertEqual(ROLE_QUEUES, {'worker': 'core', 'billing': 'billing', 'fiscal': 'fiscal'})

    def test_first_install_is_single_server_and_reruns_preserve_extraction(self):
        self.assertEqual(local_profiles(None, ''), 'billing,fiscal,network')
        self.assertIsNone(local_profiles(None, "COMPOSE_PROFILES='billing,network'\n"))
        self.assertEqual(local_profiles('network', ''), 'network')
        self.assertEqual(local_profiles('', ''), '')
        with self.assertRaises(ValueError):
            local_profiles('unrecognized', '')

    def test_exported_release_requires_explicit_source_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(ValueError):
                release_identity(root)
            (root / 'RELEASE').write_text('c' * 40 + '\n')
            self.assertEqual(release_identity(root), 'c' * 40)


@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL advisory locks required')
class SchedulerLeadershipTests(TransactionTestCase):
    def test_only_one_scheduler_leads_and_release_allows_takeover(self):
        with scheduler_lock() as first:
            self.assertTrue(first())
            with scheduler_lock() as second:
                self.assertIsNone(second)
        with scheduler_lock() as replacement:
            self.assertTrue(replacement())
