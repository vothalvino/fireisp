import copy
import json
import os
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Customer, Organization, Plan, Subscription
from core.secrets import decrypt, encrypt
from .models import ProvisioningJob, RadiusCredential, RadiusSession, Router
from .routeros import RouterError, quote
from .services import addressing, apply_plan, build_plan, enqueue, process_job, queue_rates, queue_subscription_sync, rollback, snapshot_hash


SNAPSHOT = {'addresses': [{'address': '203.0.113.1/24', 'interface': 'ether1'}], 'ppp_aaa': {'use-radius': 'no', 'accounting': 'yes', 'interim-update': '0s'}, 'radius_incoming': {'accept': 'no', 'port': '1700'}, 'active': [], 'resource': {'version': '7.21.5'}, 'wireguard': [], 'peers': []}
KEY = 'ssh-ed25519 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='


class FakeRouter:
    def __init__(self, snapshot=None):
        self.snapshot = copy.deepcopy(snapshot or SNAPSHOT)
        self.resources = {('/radius', 'unrelated'): {'address': '10.0.0.1', 'secret': 'do-not-touch'}}
        self.commands = []
        self.created = 0
        self.globals = {'/ppp/aaa': dict(self.snapshot['ppp_aaa']), '/radius/incoming': dict(self.snapshot['radius_incoming'])}

    def discover(self):
        return copy.deepcopy(self.snapshot)

    def create(self, menu, marker, values):
        if (menu, marker) in self.resources:
            return False
        self.resources[menu, marker] = dict(values)
        self.created += 1
        return True

    def rows(self, menu, props):
        rows = [dict(values) for (resource_menu, marker), values in self.resources.items() if resource_menu == menu]
        if menu == '/interface/wireguard':
            for row in rows:
                row['public-key'] = 'A' * 43 + '='
        return rows

    def run(self, command):
        self.commands.append(command)
        if ' set ' in command:
            menu, arguments = command.split(' set ', 1)
            if menu in self.globals:
                import re
                for key, value in re.findall(r'([\w-]+)="([^"]*)"', arguments):
                    self.globals[menu][key] = value
        return 'sent=3 received=3 packet-loss=0%'

    def settings(self, menu, props):
        return {key: self.globals[menu][key] for key in props}

    def remove_managed(self, menu, marker):
        self.resources.pop((menu, marker), None)


def fake_agent(operation, router_id, **kwargs):
    return {'public_key': 'A' * 43 + '=', 'configured': True, 'handshake_recent': True}


@override_settings(NETWORK_RADIUS_TOKEN='t' * 40)
class NetworkTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='ISP prueba')
        self.staff = get_user_model().objects.create_user(username='operator', password='long-test-password', is_staff=True, is_superuser=True)
        self.router = Router.objects.create(organization=self.org, name='Lab', management_host='203.0.113.1', username='lab', password_encrypted=encrypt('router-secret-never-output'), candidate_host_key=KEY, trusted_host_key=KEY, discovered_at=timezone.now(), snapshot=copy.deepcopy(SNAPSHOT), snapshot_hash=snapshot_hash(SNAPSHOT))
        self.env = patch.dict(os.environ, {'NETWORK_PUBLIC_ENDPOINT': '198.51.100.1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def plan_job(self):
        plan = build_plan(self.router)
        plan['global_approved'] = True
        return enqueue(self.router, 'apply', self.staff, plan, key='one-plan')

    def post_radius(self, route, body, token='t' * 40):
        return self.client.post(reverse('network:' + route), data=json.dumps(body), content_type='application/json', HTTP_AUTHORIZATION='Bearer ' + token)

    def credential(self, lab=True):
        self.router.provisioned_at = timezone.now()
        self.router.save()
        return RadiusCredential.objects.create(router=self.router, username='fi1-lab-abcdef12', password_encrypted=encrypt('pppoe-secret'), is_lab=lab, expires_at=timezone.now() + timedelta(minutes=5))

    def test_secret_encryption_and_selective_snapshot(self):
        self.assertNotIn('router-secret', self.router.password_encrypted)
        self.assertEqual(decrypt(self.router.password_encrypted), 'router-secret-never-output')
        self.assertNotIn('private-key', json.dumps(build_plan(self.router)))
        self.assertNotIn('password', json.dumps(build_plan(self.router)))

    def test_host_trust_requires_reviewed_fingerprint_and_staff(self):
        response = self.client.post(reverse('network:trust', args=[self.router.pk]), {'confirm': 'on', 'fingerprint': 'wrong'})
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.staff)
        self.router.trusted_host_key = ''
        self.router.save()
        self.client.post(reverse('network:trust', args=[self.router.pk]), {'confirm': 'on', 'fingerprint': 'wrong'})
        self.router.refresh_from_db()
        self.assertEqual(self.router.trusted_host_key, '')
        self.assertFalse(ProvisioningJob.objects.exists())

    def test_routeros_quote_blocks_control_and_expansion(self):
        self.assertEqual(quote('a"$b\\c'), '"a\\"\\$b\\\\c"')
        with self.assertRaises(RouterError):
            quote('x\n/system reset-configuration')

    def test_apply_is_idempotent_and_preserves_unrelated_resources(self):
        job = self.plan_job()
        api = FakeRouter()
        first = apply_plan(job, api, fake_agent)
        count = api.created
        apply_plan(job, api, fake_agent)
        self.assertEqual(api.created, count)
        self.assertFalse(first['pppoe_session'])
        self.assertEqual(api.resources['/radius', 'unrelated']['secret'], 'do-not-touch')
        self.assertNotIn('ether1', json.dumps({str(k): v for k, v in api.resources.items()}))
        self.assertTrue(all(step.get('marker', '').startswith(f'fireisp:{self.router.pk}:') for step in job.journal if step['kind'] == 'resource'))

    def test_stale_plan_rejected_before_mutation(self):
        job = self.plan_job()
        api = FakeRouter()
        api.snapshot['active'] = [{'name': 'new-subscriber'}]
        with self.assertRaisesMessage(RouterError, 'cambió'):
            apply_plan(job, api, fake_agent)
        self.assertEqual(api.created, 0)
        self.assertEqual(job.journal, [])

    def test_global_changes_need_separate_approval(self):
        job = self.plan_job()
        job.plan['global_approved'] = False
        with self.assertRaisesMessage(RouterError, 'globales'):
            apply_plan(job, FakeRouter(), fake_agent)

    def test_rollback_preserves_later_operator_change(self):
        source = self.plan_job()
        api = FakeRouter()
        apply_plan(source, api, fake_agent)
        api.globals['/ppp/aaa']['use-radius'] = 'no'
        job = enqueue(self.router, 'rollback', self.staff, {'source_job': str(source.pk)}, key='rollback')
        result = rollback(job, api, fake_agent)
        self.assertEqual(len(api.resources), 1)
        self.assertIn(('/radius', 'unrelated'), api.resources)
        self.assertEqual(api.globals['/radius/incoming']['accept'], 'no')
        self.assertTrue(result['warnings'])

    def test_routeros_zero_exit_mutation_error_is_not_success(self):
        import io
        from types import SimpleNamespace
        from network.routeros import RouterOS
        stdout = io.BytesIO(b'value rejected password=must-never-be-persisted')
        stdout.channel = SimpleNamespace(recv_exit_status=lambda: 0)
        api = RouterOS(self.router)
        api.client = SimpleNamespace(exec_command=lambda *a, **kw: (None, stdout, io.BytesIO(b'')))
        with self.assertRaises(RouterError) as error:
            api.run('/interface/eoip add name="fi1eoip"')
        self.assertNotIn('must-never-be-persisted', str(error.exception))
        self.assertIn('/interface/eoip add', str(error.exception))

    def test_routeros_creation_requires_read_back(self):
        from network.routeros import RouterOS
        api = RouterOS(self.router)
        with patch.object(api, 'managed_exists', return_value=False), patch.object(api, 'run', return_value=''):
            with self.assertRaisesMessage(RouterError, 'no confirmó'):
                api.create('/interface/bridge/port', 'fireisp:1:bridge-eoip', {'bridge': 'fi1lab', 'interface': 'fi1eoip'})

    def test_pending_commissioning_is_bounded_and_does_not_activate_billing(self):
        credential = self.credential(lab=False)
        customer = Customer.objects.create(organization=self.org, code='C2', name='Pending client', address='Address')
        plan = Plan.objects.create(organization=self.org, name='20', download_mbps=20, upload_mbps=5, price_mxn='300')
        subscription = Subscription.objects.create(customer=customer, plan=plan, status='pending', access_username=credential.username)
        credential.subscription = subscription
        credential.save()
        body = {'User-Name': credential.username, 'NAS-IP-Address': addressing(self.router.pk)['router']}
        self.assertEqual(self.post_radius('radius_authorize', body).status_code, 403)
        credential.commissioning = True
        credential.save()
        self.assertEqual(self.post_radius('radius_authorize', body).status_code, 200)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, 'pending')
        self.assertIsNone(subscription.activated_at)
        credential.expires_at = timezone.now() - timedelta(seconds=1)
        credential.save()
        self.assertEqual(self.post_radius('radius_authorize', body).status_code, 403)

    def test_idempotency_key_returns_same_durable_job(self):
        first = enqueue(self.router, 'discover', self.staff, key='discovery')
        second = enqueue(self.router, 'discover', self.staff, key='discovery')
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ProvisioningJob.objects.count(), 1)

    def test_unexpected_exception_is_redacted(self):
        job = enqueue(self.router, 'probe')
        with patch('network.services.probe_key', side_effect=RuntimeError('password=do-not-output')):
            process_job(job)
        self.assertEqual(job.status, 'failed')
        self.assertNotIn('do-not-output', job.error)

    def test_radius_requires_token_and_matches_registered_nas(self):
        credential = self.credential()
        body = {'User-Name': credential.username, 'NAS-IP-Address': addressing(self.router.pk)['router']}
        self.assertEqual(self.post_radius('radius_authorize', body, token='wrong').status_code, 401)
        self.assertEqual(self.post_radius('radius_authorize', {**body, 'NAS-IP-Address': '10.255.255.1'}).status_code, 403)
        response = self.post_radius('radius_authorize', body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['reply:Mikrotik-Rate-Limit']['value'], ['5M/10M'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        credential.expires_at = timezone.now() - timedelta(seconds=1)
        credential.save()
        self.assertEqual(self.post_radius('radius_authorize', body).status_code, 403)

    def test_subscription_cancellation_rejects_new_auth_and_queues_disconnect(self):
        credential = self.credential(lab=False)
        customer = Customer.objects.create(organization=self.org, code='C1', name='Client', address='Address')
        plan = Plan.objects.create(organization=self.org, name='20', download_mbps=20, upload_mbps=5, price_mxn='300')
        subscription = Subscription.objects.create(customer=customer, plan=plan, status='active', access_username=credential.username, paid_until=timezone.now() + timedelta(days=1))
        credential.subscription = subscription
        credential.save()
        body = {'User-Name': credential.username, 'NAS-IP-Address': addressing(self.router.pk)['router']}
        self.assertEqual(self.post_radius('radius_authorize', body).status_code, 200)
        RadiusSession.objects.create(router=self.router, username=credential.username, session_id='old-session', started_at=timezone.now())
        subscription.status = 'cancelled'
        subscription.save()
        self.assertEqual(self.post_radius('radius_authorize', body).status_code, 403)
        first = queue_subscription_sync(subscription.pk)
        second = queue_subscription_sync(subscription.pk)
        self.assertEqual(first['jobs'], second['jobs'])
        self.assertEqual(ProvisioningJob.objects.filter(action='disconnect').count(), 1)

    def test_repeated_suspend_resume_cycles_get_new_disconnect_jobs(self):
        credential = self.credential(lab=False)
        customer = Customer.objects.create(organization=self.org, code='C3', name='Cycles', address='Address')
        plan = Plan.objects.create(organization=self.org, name='20', download_mbps=20, upload_mbps=5, price_mxn='300')
        subscription = Subscription.objects.create(customer=customer, plan=plan, status='suspended', access_username=credential.username)
        credential.subscription = subscription
        credential.save()
        first = queue_subscription_sync(subscription.pk)
        self.assertEqual(first['jobs'], queue_subscription_sync(subscription.pk)['jobs'])
        subscription.status = 'active'
        subscription.save()
        queue_subscription_sync(subscription.pk)
        subscription.status = 'suspended'
        subscription.save()
        second = queue_subscription_sync(subscription.pk)
        self.assertNotEqual(first['jobs'], second['jobs'])

    def test_old_suspension_job_does_not_disconnect_a_resumed_subscription(self):
        from .services import disconnect_job
        credential = self.credential(lab=False)
        customer = Customer.objects.create(organization=self.org, code='C4', name='Resumed', address='Address')
        plan = Plan.objects.create(organization=self.org, name='20', download_mbps=20, upload_mbps=5, price_mxn='300')
        subscription = Subscription.objects.create(customer=customer, plan=plan, status='suspended', access_username=credential.username)
        credential.subscription = subscription
        credential.save()
        queued = queue_subscription_sync(subscription.pk)
        subscription.status = 'active'
        subscription.save()
        queue_subscription_sync(subscription.pk)
        with patch('network.services.call_agent') as agent:
            result = disconnect_job(ProvisioningJob.objects.get(pk=queued['jobs'][0]), FakeRouter(), agent)
        self.assertTrue(result['superseded'])
        agent.assert_not_called()

    def test_routeros_queue_rate_serializations(self):
        for value in ([5000000, 10000000], ['5000000', '10000000'], '5000000/10000000', '5M/10M', '5000k/0.01G'):
            self.assertEqual(queue_rates(value), [5000000, 10000000])
        self.assertIsNone(queue_rates('unobserved'))
        self.assertIsNone(queue_rates([5000000]))

    def test_accounting_on_without_subscriber_is_accepted_only_from_known_nas(self):
        self.credential()
        body = {'NAS-IP-Address': addressing(self.router.pk)['router'], 'Acct-Status-Type': 'Accounting-On'}
        self.assertEqual(self.post_radius('radius_accounting', body).status_code, 204)
        self.assertEqual(self.post_radius('radius_accounting', {**body, 'NAS-IP-Address': '10.1.2.3'}).status_code, 400)
        self.assertFalse(RadiusSession.objects.exists())

    def test_accounting_replay_never_reopens_or_reduces_counters(self):
        credential = self.credential()
        body = {'User-Name': {'value': [credential.username]}, 'NAS-IP-Address': {'value': [addressing(self.router.pk)['router']]}, 'Acct-Session-Id': {'value': ['0001']}, 'Framed-IP-Address': {'value': ['10.254.0.10']}}
        self.assertEqual(self.post_radius('radius_accounting', {**body, 'Acct-Status-Type': 'Start'}).status_code, 204)
        self.assertEqual(self.post_radius('radius_accounting', {**body, 'Acct-Status-Type': 'Stop', 'Acct-Input-Octets': '123', 'Acct-Input-Gigawords': '1'}).status_code, 204)
        self.post_radius('radius_accounting', {**body, 'Acct-Status-Type': 'Start', 'Acct-Input-Octets': '1'})
        session = RadiusSession.objects.get()
        self.assertIsNotNone(session.stopped_at)
        self.assertEqual(session.input_octets, 2 ** 32 + 123)
        self.assertEqual(RadiusSession.objects.count(), 1)

    def test_old_out_of_order_journal_does_not_look_like_a_recent_live_session(self):
        credential = self.credential()
        start = int((timezone.now() - timedelta(hours=3)).timestamp())
        stop = start + 60
        body = {'User-Name': credential.username, 'NAS-IP-Address': addressing(self.router.pk)['router'], 'Acct-Session-Id': 'offline-0001'}
        self.assertEqual(self.post_radius('radius_accounting', {**body, 'Acct-Status-Type': 'Stop', 'FireISP-Journal-Timestamp': str(stop), 'Acct-Input-Octets': '900'}).status_code, 204)
        self.assertEqual(self.post_radius('radius_accounting', {**body, 'Acct-Status-Type': 'Start', 'FireISP-Journal-Timestamp': str(start)}).status_code, 204)
        self.post_radius('radius_accounting', {**body, 'Acct-Status-Type': 'Interim-Update', 'FireISP-Journal-Timestamp': str(start + 30), 'Acct-Input-Octets': '100'})
        session = RadiusSession.objects.get()
        self.assertEqual(int(session.started_at.timestamp()), start)
        self.assertEqual(int(session.stopped_at.timestamp()), stop)
        self.assertEqual(int(session.updated_at.timestamp()), stop)
        self.assertEqual(session.input_octets, 900)
        self.assertLess(session.updated_at, timezone.now() - timedelta(hours=2))
        self.assertIsNotNone(session.replayed_at)


class RestrictedAgentTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import sys
        root = Path(__file__).resolve().parent.parent / 'deploy' / 'network'
        sys.path.insert(0, str(root))
        import agent
        import eoip
        cls.agent, cls.eoip = agent, eoip

    def test_invalid_radius_replacement_preserves_live_daemon(self):
        from unittest.mock import Mock, patch
        import radius_daemon
        process = Mock()
        for result in [Mock(returncode=1), radius_daemon.subprocess.TimeoutExpired('freeradius', 20)]:
            with self.subTest(result=type(result).__name__), patch.object(radius_daemon.subprocess, 'run') as check, patch.object(radius_daemon.subprocess, 'Popen') as start:
                if isinstance(result, Exception): check.side_effect = result
                else: check.return_value = result
                returned, accepted = radius_daemon.replace_daemon(process, True)
                self.assertIs(returned, process)
                self.assertFalse(accepted)
                process.terminate.assert_not_called()
                start.assert_not_called()

    def test_radius_replacement_validates_before_stopping(self):
        from unittest.mock import Mock, patch
        import radius_daemon
        events = []
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = lambda: events.append('stop')
        process.wait.side_effect = lambda **kwargs: events.append('wait')
        def validate(*args, **kwargs):
            events.append('validate')
            return Mock(returncode=0)
        def start(*args, **kwargs):
            events.append('start')
            return Mock()
        with patch.object(radius_daemon.subprocess, 'run', side_effect=validate), patch.object(radius_daemon.subprocess, 'Popen', side_effect=start):
            _, accepted = radius_daemon.replace_daemon(process, True)
        self.assertTrue(accepted)
        self.assertEqual(events, ['validate', 'stop', 'wait', 'start'])

    def test_allowlist_rejects_shell_and_paths(self):
        for request in [{'operation': 'shell', 'router_id': 1, 'command': 'id'}, {'operation': 'prepare', 'router_id': 1, 'path': '/etc/passwd'}, {'operation': 'remove', 'router_id': '../1'}, {'operation': 'prepare', 'router_id': True}]:
            with self.assertRaises(self.agent.Rejected):
                self.agent.dispatch(request)

    def test_eoip_wire_format_and_validation(self):
        frame = b'\xff' * 6 + b'\x02\x00\x00\x00\x00\x01\x88\x63' + b'payload'
        packet = self.eoip.encode(frame, 1234)
        self.assertEqual(packet[:8], b'\x20\x01\x64\x00\x00\x15\xd2\x04')
        self.assertEqual(self.eoip.decode(packet, 1234), frame)
        self.assertIsNone(self.eoip.decode(packet, 1235))
        self.assertIsNone(self.eoip.decode(packet[:-1], 1234))
        self.assertIsNone(self.eoip.decode(b'bad', 1234))

    def test_address_reservations_do_not_overlap(self):
        from network.services import addressing
        first, second = addressing(1), addressing(2)
        self.assertNotEqual(first['server'], second['server'])
        self.assertEqual(first['router_port'], 55001)
        with self.assertRaises(RouterError):
            addressing(5000)


class AccountingReplayTests(SimpleTestCase):
    def setUp(self):
        import sys
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'deploy' / 'network'))
        from accounting_replay import AccountingReplay, parse_detail_block
        self.Replay, self.parse = AccountingReplay, parse_detail_block
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / '20260906.detail'
        self.block = b'Sun Sep  6 01:00:00 2026\n\tAcct-Status-Type = Start\n\tUser-Name = "fi1-lab-abcdef12"\n\tAcct-Session-Id = "0001"\n\tNAS-IP-Address = 10.253.0.6\n\tTimestamp = 1788656400\n\tAcct-Delay-Time = 2\n\n'

    class Opener:
        def __init__(self, statuses):
            self.statuses, self.requests = list(statuses), []

        def open(self, request, timeout):
            from contextlib import nullcontext
            from types import SimpleNamespace
            self.requests.append(request)
            status = self.statuses.pop(0) if self.statuses else 204
            if isinstance(status, Exception):
                raise status
            return nullcontext(SimpleNamespace(status=status))

    def replay(self, opener):
        return self.Replay('http://127.0.0.1:18000/network/radius', 't' * 40, _directory=Path(self.directory.name), _opener=opener)

    def test_callback_failure_retains_block_and_durable_cursor_avoids_duplicates(self):
        self.path.write_bytes(self.block)
        opener = self.Opener([503, 204])
        replay = self.replay(opener)
        self.assertTrue(replay.replay_once()['error'])
        self.assertEqual(replay.replay_once()['processed'], 1)

        self.assertEqual(self.replay(opener).replay_once()['processed'], 0)
        self.assertEqual(len(opener.requests), 2)
        payload = json.loads(opener.requests[-1].data)
        self.assertEqual(int(payload['FireISP-Journal-Timestamp']), 1788656400)

    def test_incomplete_block_waits_for_append(self):
        self.path.write_bytes(self.block[:-1])
        opener = self.Opener([204])
        replay = self.replay(opener)
        self.assertEqual(replay.replay_once()['processed'], 0)
        self.assertFalse(opener.requests)
        with self.path.open('ab') as output:
            output.write(b'\n')
        self.assertEqual(replay.replay_once()['processed'], 1)

    def test_parser_never_forwards_credentials_or_unknown_attributes(self):
        block = self.block.rstrip(b'\n') + b'\n\tUser-Password = "never-forward-this-secret"\n\tNAS-Identifier = "untrusted-identity"\n\n'
        data = self.parse(block)
        self.assertNotIn('never-forward-this-secret', json.dumps(data))
        self.assertNotIn('User-Password', data)
        self.assertNotIn('NAS-Identifier', data)
        self.assertEqual(data['NAS-IP-Address'], '10.253.0.6')

    def test_redirect_does_not_advance_or_change_the_callback_destination(self):
        self.path.write_bytes(self.block)
        opener = self.Opener([302, 204])
        replay = self.replay(opener)
        self.assertTrue(replay.replay_once()['error'])
        self.assertEqual(replay.replay_once()['processed'], 1)
        self.assertTrue(all(req.full_url == 'http://127.0.0.1:18000/network/radius/accounting/' for req in opener.requests))

    def test_replaced_journal_is_replayed_safely(self):
        self.path.write_bytes(self.block)
        opener = self.Opener([204, 204])
        replay = self.replay(opener)
        self.assertEqual(replay.replay_once()['processed'], 1)
        replacement = Path(self.directory.name) / 'replacement'
        replacement.write_bytes(self.block.replace(b'0001', b'0002'))
        replacement.replace(self.path)
        self.assertEqual(replay.replay_once()['processed'], 1)


class EntitlementCapacityTests(SimpleTestCase):
    def setUp(self):
        import sys
        import tempfile
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'deploy' / 'network'))
        import agent
        self.agent = agent
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.radius = Path(self.directory.name)
        directory_patch = patch.object(agent, 'RADIUS', self.radius)
        health_patch = patch.object(agent, 'radius_listener_health', return_value={'radius_ready': False})
        directory_patch.start()
        health_patch.start()
        self.addCleanup(directory_patch.stop)
        self.addCleanup(health_patch.stop)

    def entries(self, count, *, wide=False):
        return [{'username': f'customer-{i:05d}'.ljust(64, 'x') if wide else f'customer-{i:05d}', 'password': 'A' * 123 + '"\\$Z?' if wide else 'p' * 32, 'router_id': 1, 'upload_mbps': 10, 'download_mbps': 100, 'expires_at': None} for i in range(count)]

    def artifacts(self):
        return {path.name: path.read_bytes() for path in self.radius.iterdir()}

    def test_serialized_twenty_thousand_entries_publish_fully_and_idempotently(self):
        import io
        from .agent_client import encode_request
        entries = self.entries(20_000, wide=True)
        payload = encode_request('sync_entitlements', 1, entries=entries)
        self.assertGreater(len(payload), 2 * 1024 * 1024)
        result = self.agent.dispatch(self.agent.read_request(io.BytesIO(payload)))
        self.assertEqual(result['confirmed_accounts'], 20_000)
        content = (self.radius / 'entitlements').read_text()
        self.assertEqual(content.count('Cleartext-Password :='), 20_000)
        self.assertIn('"' + entries[-1]['username'] + '" ', content)
        generation = (self.radius / 'generation').read_bytes()
        self.agent.sync_entitlements(1, entries)
        self.assertEqual((self.radius / 'generation').read_bytes(), generation)
        before = self.artifacts()
        entries[-1]['username'] = entries[0]['username']
        with self.assertRaises(self.agent.Rejected):
            self.agent.sync_entitlements(1, entries)
        self.assertEqual(self.artifacts(), before)

    def test_entry_limit_accepts_twenty_five_thousand_and_rejects_one_more(self):
        entries = self.entries(25_000)
        self.assertEqual(self.agent.sync_entitlements(1, entries)['confirmed_accounts'], 25_000)
        before = self.artifacts()
        entries.append({**entries[-1], 'username': 'overflow'})
        with self.assertRaises(self.agent.Rejected):
            self.agent.sync_entitlements(1, entries)
        self.assertEqual(self.artifacts(), before)

    def test_ipc_byte_limit_includes_utf8_and_framing_before_socket_open(self):
        import io
        from . import agent_client
        entry = self.entries(1)[0]
        entry['password'] = '🔐' * 8
        payload = agent_client.encode_request('sync_entitlements', 1, entries=[entry])
        self.assertGreater(len(payload), len(payload.decode('utf-8')))
        with patch.object(agent_client, 'MAX_REQUEST_BYTES', len(payload)), patch.object(self.agent, 'MAX_REQUEST_BYTES', len(payload)):
            self.assertEqual(agent_client.encode_request('sync_entitlements', 1, entries=[entry]), payload)
            self.assertEqual(self.agent.read_request(io.BytesIO(payload))['entries'][0]['password'], entry['password'])
            with self.assertRaises(self.agent.Rejected):
                self.agent.read_request(io.BytesIO(payload[:-1] + b' \n'))
            with self.assertRaises(self.agent.Rejected):
                self.agent.read_request(io.BytesIO(payload[:-1]))
        with patch.object(agent_client, 'MAX_REQUEST_BYTES', len(payload) - 1), patch.object(agent_client.socket, 'socket') as connect:
            with self.assertRaises(agent_client.AgentError):
                agent_client.call_agent('sync_entitlements', 1, entries=[entry])
            connect.assert_not_called()

    def test_rendered_file_byte_limit_preserves_previous_generation(self):
        entries = self.entries(1)
        entries[0]['password'] = '🔐' * 128
        self.agent.sync_entitlements(1, entries)
        before = self.artifacts()
        byte_count = len(before['entitlements'])
        self.assertGreater(byte_count, len(before['entitlements'].decode('utf-8')))
        with patch.object(self.agent, 'MAX_ENTITLEMENTS_BYTES', byte_count):
            self.agent.sync_entitlements(1, entries)
        before = self.artifacts()
        with patch.object(self.agent, 'MAX_ENTITLEMENTS_BYTES', byte_count - 1):
            with self.assertRaises(self.agent.Rejected):
                self.agent.sync_entitlements(1, entries)
        self.assertEqual(self.artifacts(), before)
