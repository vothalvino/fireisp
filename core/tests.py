from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.utils import timezone
from billing.models import Invoice
from billing.services import receive_payment
from compliance.services import cancel_subscription
from operations.models import WorkOrder
from operations.services import complete_work_order
from .models import ActivationToken, Customer, Notification, Organization, Plan, Subscription
from .secrets import decrypt, encrypt
from .services import activate_subscription, audit, invite, publish
from .tasks import deliver_outbox

@override_settings(STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class LifecycleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('admin', password='a-secure-testing-passphrase')
        cls.org = Organization.objects.create(name='Demo', demo_mode=True)
        cls.customer = Customer.objects.create(organization=cls.org, name='Ficticio', code='TEST-1', address='Ficticio')
        cls.plan = Plan.objects.create(organization=cls.org, name='30', download_mbps=30, upload_mbps=10, price_mxn=Decimal('399'))
        cls.sub = Subscription.objects.create(customer=cls.customer, plan=cls.plan, access_username='test@example')

    def test_payment_installation_activation_cancel_lifecycle(self):
        payment = receive_payment(self.customer, '399', 'cash', self.admin, 'lifecycle-1')
        self.assertEqual(payment.available, Decimal('399'))
        self.assertFalse(Invoice.objects.exists())
        order = WorkOrder.objects.create(subscription=self.sub, kind='installation')
        with self.assertRaises(ValidationError): complete_work_order(order.pk, self.admin, '')
        complete_work_order(order.pk, self.admin, 'Prueba de laboratorio con aceptación registrada')
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'active')
        self.assertGreater(self.sub.paid_until, self.sub.activated_at)
        self.assertEqual(Invoice.objects.get().balance, 0)
        activate_subscription(self.sub.pk, self.admin)
        self.assertEqual(Invoice.objects.count(), 1)
        cancellation = cancel_subscription(self.sub.pk, self.admin, 'portal')
        self.assertEqual(cancellation.pk, cancel_subscription(self.sub.pk, self.admin, 'portal').pk)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, 'cancelled')
        self.assertEqual(Invoice.objects.count(), 1)
        deliver_outbox()
        deliver_outbox()
        self.assertEqual(Notification.objects.count(), 1)

    def test_production_activation_fails_without_evidence(self):
        self.org.demo_mode = False; self.org.save()
        with self.assertRaises(ValidationError): activate_subscription(self.sub.pk, self.admin)
        self.sub.refresh_from_db()
        self.assertIsNone(self.sub.activated_at)
        self.assertFalse(Invoice.objects.exists())

    def test_staff_pages_render(self):
        audit(None, 'system.initialized', 'test')
        self.client.force_login(self.admin)
        for path in ['/', '/customers/', '/customers/new/', f'/customers/{self.customer.pk}/', '/plans/', '/plans/new/', '/settings/', '/staff/new/', '/audit/', '/billing/', '/billing/payments/new/', '/billing/cash/', '/billing/bank/', '/fiscal/', '/fiscal/settings/', '/network/', '/network/add/', '/operations/', '/compliance/']:
            with self.subTest(path=path): self.assertEqual(self.client.get(path).status_code, 200)

    def test_portal_isolation_support_privacy_cancel(self):
        user = get_user_model().objects.create_user('customer', password='a-secure-testing-passphrase')
        self.customer.user = user; self.customer.save()
        other = Customer.objects.create(organization=self.org, code='TEST-2', name='Otro', address='Ficticio')
        other_sub = Subscription.objects.create(customer=other, plan=self.plan, access_username='other')
        self.client.force_login(user)
        for path in ['/portal/', '/portal/payments/', '/portal/support/', '/portal/privacy/']:
            with self.subTest(path=path): self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(f'/portal/service/{other_sub.pk}/cancel/', {'confirm': 'on'}).status_code, 404)
        response = self.client.post('/portal/support/', {'subscription': other_sub.pk, 'kind': 'billing', 'subject': 'Test', 'description': 'Test'})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(other.tickets.exists())
        self.client.post('/portal/support/', {'subscription': self.sub.pk, 'kind': 'billing', 'subject': 'Cobro', 'description': 'Solicito aclaración'})
        self.assertEqual(self.customer.tickets.count(), 1)
        self.client.post('/portal/privacy/', {'request_type': 'access', 'description': 'Mis datos'})
        self.assertEqual(self.customer.arco_requests.count(), 1)
        self.assertEqual(self.client.get('/settings/').status_code, 403)
        result = self.client.get('/lookup/customer/').json()
        self.assertEqual([row['id'] for row in result['results']], [self.customer.pk])
        result = self.client.get('/lookup/subscription/').json()
        self.assertEqual([row['id'] for row in result['results']], [self.sub.pk])

    def test_one_use_invitation_and_password_policy(self):
        user = get_user_model().objects.create_user('invitee', is_active=False)
        token = invite(user)
        self.assertNotIn(token, str(list(ActivationToken.objects.values())))
        response = self.client.post(f'/activate/{token}/', {'new_password1': '123', 'new_password2': '123'})
        self.assertEqual(response.status_code, 200)
        response = self.client.post(f'/activate/{token}/', {'new_password1': 'Unique-fantastic-passphrase-87', 'new_password2': 'Unique-fantastic-passphrase-87'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(f'/activate/{token}/').status_code, 410)

    def test_invalid_lookup_selection_renders_validation_without_writing(self):
        from billing.models import Payment
        self.client.force_login(self.admin)
        for value in ['not-an-id', '9' * 100]:
            with self.subTest(value=value):
                response = self.client.post('/billing/payments/new/', {'customer': value, 'amount': '10.00', 'method': 'cash', 'idempotency_key': 'invalid-lookup'})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context['form'].errors)
        self.assertFalse(Payment.objects.exists())

    def test_outage_lookup_preserves_multiple_affected_services(self):
        from operations.models import Outage
        other = Subscription.objects.create(customer=self.customer, plan=self.plan, access_username='second-service')
        self.client.force_login(self.admin)
        response = self.client.get('/operations/outage/new/')
        self.assertContains(response, 'multiple')
        response = self.client.post('/operations/outage/new/', {'organization': self.org.pk, 'title': 'Falla de prueba', 'started_at': '2026-09-05T10:00', 'subscriptions': [self.sub.pk, other.pk]})
        self.assertEqual(response.status_code, 302)
        outage = Outage.objects.get()
        self.assertEqual(set(outage.subscriptions.values_list('pk', flat=True)), {self.sub.pk, other.pk})
        response = self.client.get(f'/operations/outage/{outage.pk}/edit/')
        self.assertContains(response, f'value="{self.sub.pk}" selected')
        self.assertContains(response, f'value="{other.pk}" selected')

    def test_csrf_and_roles(self):
        client = Client(enforce_csrf_checks=True); client.force_login(self.admin)
        self.assertEqual(client.post('/plans/new/', {}).status_code, 403)
        staff = get_user_model().objects.create_user('support', is_staff=True)
        staff.groups.add(Group.objects.create(name='Soporte'))
        self.client.force_login(staff)
        self.assertEqual(self.client.get('/operations/').status_code, 200)
        for path in ['/billing/', '/fiscal/', '/network/', '/settings/', '/admin/', '/staff/new/']:
            self.assertEqual(self.client.get(path).status_code, 403)

    def test_secret_encryption_audit_redaction(self):
        value = 'private-value-for-test'
        encrypted = encrypt(value)
        self.assertNotIn(value, encrypted)
        self.assertEqual(decrypt(encrypted), value)
        event = audit(self.admin, 'test', self.sub, {'password': value, 'nested': {'token': value}})
        self.assertNotIn(value, str(event.details))
        with self.assertRaises(ValidationError): event.save()

    def test_outbox_failure_retries_without_exposing_payload(self):
        publish('bad', 'unsupported', {'token': 'never-log-me'})
        self.assertEqual(deliver_outbox(), 0)
        from .models import OutboxEvent
        event = OutboxEvent.objects.get(key='bad')
        self.assertIsNone(event.delivered_at)
        self.assertGreater(event.available_at, timezone.now())
        self.assertNotIn('never-log-me', event.error)

    def test_login_throttles_repeated_failures(self):
        cache.clear()
        for _ in range(10): self.client.post('/login/', {'username': 'missing', 'password': 'bad'})
        self.assertEqual(self.client.post('/login/', {'username': 'missing', 'password': 'bad'}).status_code, 429)
        cache.clear()
