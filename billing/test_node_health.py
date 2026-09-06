from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from core.models import HealthCheck
from network.models import NetworkNode, RadiusCredential, Router
from . import tests as fixtures
from .services import apply_suspension, propose_suspension, review_suspension, suspension_block


class SuspensionNetworkNodeTests(TestCase):
    def setUp(self):
        fixtures.SuspensionTests.setUp(self)
        self.node = NetworkNode.objects.create(pk='cuauhtemoc', name='Cuauhtémoc')
        self.router = Router.objects.create(organization=self.organization, network_node=self.node,
            name='Router remoto', management_host='192.0.2.17', username='test')
        self.credential = RadiusCredential.objects.create(subscription=self.subscription, router=self.router,
            username='customer-at-remote-node', password_encrypted='unused-in-health-check')

    def test_primary_health_cannot_authorize_suspension_on_unhealthy_assigned_node(self):
        self.assertEqual(self.health.code, 'network_sync')
        for index, kwargs in enumerate((None, {'status': 'failed'}, {'status': 'ok', 'stale': True})):
            with self.subTest(node_health=kwargs):
                HealthCheck.objects.filter(code=self.node.health_code).delete()
                if kwargs:
                    check = HealthCheck.objects.create(code=self.node.health_code, status=kwargs['status'])
                    if kwargs.get('stale'):
                        HealthCheck.objects.filter(pk=check.pk).update(checked_at=timezone.now()-timedelta(seconds=121))
                proposal = propose_suspension(self.subscription, self.actor, f'remote-block-{index}')
                review_suspension(proposal, True, 'Revisión del vencimiento.', self.actor)
                result = apply_suspension(proposal, self.actor)
                self.assertFalse(result.applied)
                self.subscription.refresh_from_db()
                self.assertEqual(self.subscription.status, 'active')
                self.assertIn('Sin sincronización', result.detail)

    def test_healthy_assigned_node_allows_suspension_when_primary_is_down(self):
        self.health.status = 'failed'
        self.health.save(update_fields=['status'])
        HealthCheck.objects.create(code=self.node.health_code, status='ok')
        self.assertEqual(suspension_block(self.subscription, self.policy), '')
        proposal = propose_suspension(self.subscription, self.actor, 'healthy-remote')
        review_suspension(proposal, True, 'Vigencia revisada.', self.actor)
        result = apply_suspension(proposal, self.actor)
        self.assertTrue(result.applied)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, 'suspended')

    def test_reassigning_router_rechecks_current_node_health(self):
        HealthCheck.objects.create(code=self.node.health_code, status='ok')
        proposal = propose_suspension(self.subscription, self.actor, 'node-moved')
        review_suspension(proposal, True, 'Revisión antes del traslado.', self.actor)
        replacement = NetworkNode.objects.create(pk='regional', name='Regional')
        self.router.network_node = replacement
        self.router.save(update_fields=['network_node'])
        self.assertFalse(apply_suspension(proposal, self.actor).applied)
