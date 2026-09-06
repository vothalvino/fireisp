import hashlib
import io
import json
import uuid
from datetime import timedelta
from threading import Event, Thread
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections, close_old_connections
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from core.models import Organization
from core.secrets import encrypt
from .execution import LeaseLost, NodeBusy, node_execution, recover_interrupted_jobs
from .models import NetworkNode, ProvisioningJob, RadiusCredential, RadiusSession, Router
from .routeros import RouterError
from .services import addressing, build_plan, enqueue, process_job, retry_reviewed_job, snapshot_hash, sync_confirmed_entitlements
from .tests import KEY, SNAPSHOT


@override_settings(NETWORK_NODE_ID='primary', NETWORK_RADIUS_TOKEN='p' * 40)
class NetworkNodeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Nodes demo')
        self.primary, _ = NetworkNode.objects.get_or_create(pk='primary')
        self.remote = NetworkNode.objects.create(pk='north', public_endpoint='198.51.100.23', radius_token_digest=hashlib.sha256(('r' * 40).encode()).hexdigest())
        self.router = Router.objects.create(organization=self.org, name='Primary', management_host='203.0.113.2', username='test', trusted_host_key=KEY, discovered_at=timezone.now(), snapshot=SNAPSHOT, snapshot_hash=snapshot_hash(SNAPSHOT), provisioned_at=timezone.now())
        self.remote_router = Router.objects.create(organization=self.org, network_node=self.remote, name='North', management_host='203.0.113.3', username='test', trusted_host_key=KEY, discovered_at=timezone.now(), snapshot=SNAPSHOT, snapshot_hash=snapshot_hash(SNAPSHOT), provisioned_at=timezone.now())
        for router, username in [(self.router, 'primary-user'), (self.remote_router, 'north-user')]:
            RadiusCredential.objects.create(router=router, username=username, password_encrypted=encrypt('long-lab-password'), is_lab=True)

    def radius(self, router, token, route='radius_authorize'):
        body = {'NAS-IP-Address': addressing(router.pk)['router'], 'User-Name': 'north-user' if router == self.remote_router else 'primary-user', 'Acct-Session-Id': 'scope-test', 'Acct-Status-Type': 'Start'}
        return self.client.post(reverse('network:' + route), data=json.dumps(body), content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + token, HTTP_X_NETWORK_NODE_ID='north')

    def test_legacy_token_is_primary_only_and_spoofed_header_does_not_expand_scope(self):
        self.assertEqual(self.radius(self.router, 'p' * 40).status_code, 200)
        self.assertEqual(self.radius(self.remote_router, 'p' * 40).status_code, 403)
        self.assertEqual(self.radius(self.remote_router, 'p' * 40, 'radius_accounting').status_code, 400)
        self.assertFalse(RadiusSession.objects.exists())

    def test_registered_node_token_authorizes_and_accounts_only_its_routers(self):
        self.assertEqual(self.radius(self.remote_router, 'r' * 40).status_code, 200)
        self.assertEqual(self.radius(self.router, 'r' * 40).status_code, 403)
        self.assertEqual(self.radius(self.router, 'r' * 40, 'radius_accounting').status_code, 400)
        self.assertEqual(self.radius(self.remote_router, 'r' * 40, 'radius_accounting').status_code, 204)
        self.assertEqual(RadiusSession.objects.get().router_id, self.remote_router.pk)

    def test_rotating_primary_token_disables_legacy_token(self):
        self.primary.radius_token_digest = hashlib.sha256(('n' * 40).encode()).hexdigest()
        self.primary.save()
        self.assertEqual(self.radius(self.router, 'p' * 40).status_code, 401)
        self.assertEqual(self.radius(self.router, 'n' * 40).status_code, 200)

    def test_each_local_snapshot_contains_only_assigned_credentials(self):
        with patch('network.services.call_agent'):
            for node_id, username in [('primary', 'primary-user'), ('north', 'north-user')]:
                with self.settings(NETWORK_NODE_ID=node_id):
                    observed = []
                    def agent(operation, router_id, **payload):
                        observed.extend(payload['entries'])
                        return {'radius_ready': True}
                    result = sync_confirmed_entitlements(agent)
                    self.assertEqual(result['network_node_id'], node_id)
                    self.assertEqual([row['username'] for row in observed], [username])

    def test_remote_plan_uses_registered_endpoint_and_pins_node(self):
        self.remote_router.provisioned_at = None
        with patch.dict('os.environ', {'NETWORK_PUBLIC_ENDPOINT': '198.51.100.1'}):
            plan = build_plan(self.remote_router)
        self.assertEqual(plan['endpoint'], '198.51.100.23')
        self.assertEqual(plan['network_node_id'], 'north')

    def test_wrong_node_cannot_execute_job(self):
        job = enqueue(self.remote_router, 'probe')
        with patch('network.services.probe_key') as probe:
            with self.assertRaises(RouterError):
                process_job(job)
            probe.assert_not_called()
        job.refresh_from_db()
        self.assertEqual(job.status, 'pending')

    def test_claim_is_idempotent_and_records_owner_generation(self):
        job = enqueue(self.router, 'probe')
        with patch('network.services.probe_key', return_value=KEY) as probe:
            process_job(job)
            process_job(job)
            self.assertEqual(probe.call_count, 1)
        job.refresh_from_db()
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.attempts, 1)
        self.assertIsNotNone(job.worker_token)
        self.assertGreater(job.worker_generation, 0)

    def test_live_durable_lease_cannot_be_taken_over(self):
        self.primary.worker_token = uuid.uuid4()
        self.primary.lease_expires_at = timezone.now() + timedelta(minutes=2)
        self.primary.save()
        with self.assertRaises(NodeBusy):
            with node_execution():
                self.fail('A live lease was stolen')

    def test_superseded_owner_is_fenced_and_cannot_clear_replacement(self):
        replacement = uuid.uuid4()
        with node_execution() as lease:
            NetworkNode.objects.filter(pk='primary').update(worker_token=replacement, generation=lease.generation + 1)
            with self.assertRaises(LeaseLost):
                lease.check()
        self.primary.refresh_from_db()
        self.assertEqual(self.primary.worker_token, replacement)

    def test_interrupted_job_is_quarantined_without_replay_and_other_node_untouched(self):
        old = enqueue(self.router, 'probe')
        foreign = enqueue(self.remote_router, 'probe')
        next_job = enqueue(self.router, 'probe')
        ProvisioningJob.objects.filter(pk__in=[old.pk, foreign.pk]).update(status='running', worker_token=uuid.uuid4(), started_at=timezone.now() - timedelta(minutes=6))
        with node_execution() as lease:
            self.assertEqual(recover_interrupted_jobs(lease), 1)
        with patch('network.services.probe_key') as probe:
            process_job(next_job)
            probe.assert_not_called()
        old.refresh_from_db()
        foreign.refresh_from_db()
        self.router.refresh_from_db()
        self.assertEqual(old.status, 'failed')
        self.assertEqual(foreign.status, 'running')
        self.assertTrue(self.router.execution_blocked)
        retry_reviewed_job(old)
        self.router.refresh_from_db()
        self.assertFalse(self.router.execution_blocked)

    def test_node_registration_reads_secret_stdin_and_rejects_endpoint_change(self):
        output = io.StringIO()
        secret = 'separate-node-token-12345678901234567890'
        with patch('sys.stdin', io.StringIO(secret)):
            call_command('register_network_node', 'east', endpoint='198.51.100.99', radius_token_stdin=True, stdout=output)
        node = NetworkNode.objects.get(pk='east')
        self.assertEqual(node.radius_token_digest, hashlib.sha256(secret.encode()).hexdigest())
        self.assertNotIn(secret, output.getvalue())
        with patch('sys.stdin', io.StringIO('z' * 40)):
            with self.assertRaises(CommandError):
                call_command('register_network_node', 'north', endpoint='198.51.100.100', radius_token_stdin=True, stdout=output)
        self.remote.refresh_from_db()
        self.assertEqual(self.remote.public_endpoint, '198.51.100.23')

    def test_worker_claims_only_assigned_node_jobs(self):
        from types import SimpleNamespace
        local = enqueue(self.router, 'probe')
        remote = enqueue(self.remote_router, 'probe')
        response = SimpleNamespace(read=lambda limit: b'{"application_ready": true, "database_ready": true}')
        with self.settings(NETWORK_NODE_ID='north'), patch('network.management.commands.run_network_jobs.heartbeat'), patch('network.management.commands.run_network_jobs.sync_confirmed_entitlements', return_value={'radius_ready': True}), patch('network.management.commands.run_network_jobs.urllib.request.urlopen') as urlopen, patch('network.services.probe_key', return_value=KEY):
            urlopen.return_value.__enter__.return_value = response
            call_command('run_network_jobs', once=True, stdout=io.StringIO())
        local.refresh_from_db()
        remote.refresh_from_db()
        self.assertEqual(local.status, 'pending')
        self.assertEqual(remote.status, 'succeeded')
        from core.models import HealthCheck
        self.assertEqual(HealthCheck.objects.get().code, 'network_sync:north')

    def test_health_codes_do_not_overwrite_another_node(self):
        self.assertEqual(self.primary.health_code, 'network_sync')
        self.assertEqual(self.remote.health_code, 'network_sync:north')


@override_settings(NETWORK_NODE_ID='primary')
class NetworkNodePostgresTests(TransactionTestCase):
    def setUp(self):
        NetworkNode.objects.get_or_create(pk='primary')

    @skipUnlessDBFeature('has_select_for_update')
    def test_two_process_connections_cannot_own_one_node(self):
        ready, finish = Event(), Event()
        errors = []
        def owner():
            close_old_connections()
            try:
                with node_execution():
                    ready.set()
                    if not finish.wait(10):
                        raise RuntimeError('Test owner wait expired')
            except Exception as exc:
                errors.append(exc)
                ready.set()
            finally:
                connections.close_all()
        thread = Thread(target=owner)
        thread.start()
        try:
            self.assertTrue(ready.wait(10))
            self.assertEqual(errors, [])
            with self.assertRaises(NodeBusy):
                with node_execution():
                    self.fail('Two owners acquired one node')
        finally:
            finish.set()
            thread.join(10)
        self.assertEqual(errors, [])
        with node_execution() as lease:
            lease.check()

    @skipUnlessDBFeature('has_select_for_update')
    def test_competing_workers_do_not_duplicate_a_remote_effect(self):
        org = Organization.objects.create(name='Concurrent node')
        router = Router.objects.create(organization=org, name='Race', management_host='203.0.113.10', username='test')
        job = enqueue(router, 'probe')
        entered, finish = Event(), Event()
        errors = []
        calls = []
        def probe(router):
            calls.append(router.pk)
            entered.set()
            if not finish.wait(10):
                raise RuntimeError('Test probe wait expired')
            return KEY
        def owner():
            close_old_connections()
            try:
                process_job(ProvisioningJob.objects.select_related('router').get(pk=job.pk))
            except Exception as exc:
                errors.append(exc)
                entered.set()
            finally:
                connections.close_all()
        with patch('network.services.probe_key', side_effect=probe):
            thread = Thread(target=owner)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                self.assertEqual(errors, [])
                process_job(job)
                self.assertEqual(calls, [router.pk])
            finally:
                finish.set()
                thread.join(10)
        self.assertEqual(errors, [])
        job.refresh_from_db()
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.attempts, 1)
