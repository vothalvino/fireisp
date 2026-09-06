from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import Customer, Organization, Plan, Subscription
from .models import Equipment, Outage, OutageCredit, Sector, Site, Ticket, WorkOrder
from .services import calculate_outage_credit, complete_work_order, has_billing_dispute_hold, record_outage_credit, require_installation_readiness, resolve_ticket


class OperationsTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username="ops", is_staff=True)
        self.staff.groups.add(Group.objects.get_or_create(name="Soporte")[0])
        self.org = Organization.objects.create(name="Prueba", demo_mode=True)
        self.customer = Customer.objects.create(organization=self.org, code="OPS1", name="Cliente", address="Cuauhtémoc")
        self.plan = Plan.objects.create(organization=self.org, name="50 Mbps", download_mbps=50, upload_mbps=10, price_mxn=Decimal("310"))
        self.subscription = Subscription.objects.create(customer=self.customer, plan=self.plan, access_username="ops1")
        self.other_subscription = Subscription.objects.create(customer=self.customer, plan=self.plan, access_username="ops2")
        self.start = datetime(2026, 1, 1, tzinfo=dt_timezone.utc)
        self.end = datetime(2026, 2, 1, tzinfo=dt_timezone.utc)

    def test_billing_dispute_blocks_only_affected_subscription_until_resolution(self):
        ticket = Ticket.objects.create(customer=self.customer, subscription=self.subscription, kind="billing", subject="Pago", description="Pagado")
        self.assertTrue(has_billing_dispute_hold(self.subscription))
        self.assertFalse(has_billing_dispute_hold(self.other_subscription))
        with self.assertRaises(ValidationError):
            resolve_ticket(ticket.pk, self.staff, " ")
        self.assertTrue(has_billing_dispute_hold(self.subscription))
        resolve_ticket(ticket.pk, self.staff, "Pago conciliado y cliente informado.")
        self.assertFalse(has_billing_dispute_hold(self.subscription))

    def test_account_wide_billing_dispute_blocks_all_services(self):
        Ticket.objects.create(customer=self.customer, kind="billing", subject="Cuenta", description="Aclaración")
        self.assertTrue(has_billing_dispute_hold(self.subscription))
        self.assertTrue(has_billing_dispute_hold(self.other_subscription))

    def test_ticket_rejects_cross_customer_service(self):
        other = Customer.objects.create(organization=self.org, code="OTHER", name="Otro", address="Otro")
        ticket = Ticket(customer=other, subscription=self.subscription, subject="Cruce", description="Incorrecto")
        with self.assertRaises(ValidationError):
            ticket.full_clean()

    def test_work_completion_requires_evidence_and_is_idempotent(self):
        work = WorkOrder.objects.create(subscription=self.subscription)
        with self.assertRaises(ValidationError):
            complete_work_order(work.pk, self.staff)
        with patch("core.services.activate_subscription") as activate:
            complete_work_order(work.pk, self.staff, "CPE instalado, PPPoE autenticado, 50/10 Mbps medidos y cliente acepta.")
            complete_work_order(work.pk, self.staff, "Segundo intento")
            activate.assert_called_once_with(self.subscription.pk, self.staff)
        work.refresh_from_db()
        self.assertEqual(work.status, "completed")
        self.assertIsNotNone(work.completed_at)

    def test_work_activation_failure_does_not_complete_order(self):
        work = WorkOrder.objects.create(subscription=self.subscription)
        with patch("core.services.activate_subscription", side_effect=ValidationError("Producción bloqueada")):
            with self.assertRaises(ValidationError):
                complete_work_order(work.pk, self.staff, "Pruebas")
        work.refresh_from_db()
        self.assertEqual(work.status, "scheduled")

    def test_credit_uses_actual_31_day_period_and_clips_overlap(self):
        amounts = calculate_outage_credit("310", self.start - timedelta(days=2), self.start + timedelta(days=1), self.start, self.end)
        self.assertEqual(amounts, {"proportional": Decimal("10.00"), "bonus": Decimal("2.00"), "total": Decimal("12.00")})
        self.assertEqual(calculate_outage_credit("310", self.end, self.end + timedelta(days=1), self.start, self.end)["total"], Decimal("0.00"))

    def test_invalid_credit_intervals_fail(self):
        with self.assertRaises(ValidationError):
            calculate_outage_credit("310", self.end, self.start, self.start, self.end)

    def test_outage_credit_is_idempotent_and_not_marked_applied(self):
        outage = Outage.objects.create(organization=self.org, title="Falla", started_at=self.start, ended_at=self.start + timedelta(days=1), provider_attributable=True, attribution_evidence="Falla equipo propio")
        outage.subscriptions.add(self.subscription)
        credit = record_outage_credit(outage, self.subscription, self.start, self.end, self.staff)
        again = record_outage_credit(outage, self.subscription, self.start, self.end, self.staff)
        self.assertEqual(credit.pk, again.pk)
        self.assertEqual(OutageCredit.objects.count(), 1)
        self.assertIsNone(credit.applied_at)

    def test_radio_eirp_and_homologation_evidence_validation(self):
        item = Equipment(organization=self.org, serial_number="SERIAL", model="CPE", tx_power_dbm=Decimal("25"), antenna_gain_dbi=Decimal("16"), cable_loss_db=Decimal("1"), allowed_eirp_dbm=Decimal("36"))
        self.assertEqual(item.eirp_dbm, Decimal("40"))
        with self.assertRaises(ValidationError):
            item.full_clean()
        item.tx_power_dbm = Decimal("20")
        item.homologation_verified = True
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_production_installation_needs_field_evidence_and_recent_real_session(self):
        from network.models import RadiusCredential, RadiusSession, Router
        self.org.demo_mode = False
        self.org.save()
        with self.assertRaises(ValidationError):
            require_installation_readiness(self.subscription)
        WorkOrder.objects.create(subscription=self.subscription, status="completed", completed_at=timezone.now(), completion_evidence="Velocidad medida y aceptación documentada")
        site = Site.objects.create(organization=self.org, name="Sitio", address="Centro", owner_permission="Arrendamiento vigente", permit_status="approved", permit_evidence="Expediente local revisado")
        sector = Sector.objects.create(site=site, name="Sector", frequency_mhz=5800, channel_width_mhz=20, regulatory_basis="Perfil de prueba: revisar parámetros por banda antes de uso real")
        Equipment.objects.create(organization=self.org, serial_number="INSTALLED", model="Equipo de prueba", status="installed", subscription=self.subscription, sector=sector, homologation_verified=True, homologation_certificate="CERT-TEST", homologation_evidence="Evidencia de prueba", tx_power_dbm=Decimal("15"), antenna_gain_dbi=Decimal("10"), allowed_eirp_dbm=Decimal("30"))
        router = Router.objects.create(organization=self.org, name="Router", management_host="192.0.2.1", username="test", is_lab=False)
        RadiusCredential.objects.create(subscription=self.subscription, router=router, username=self.subscription.access_username, password_encrypted="fixture", enabled=True, commissioning=True, expires_at=timezone.now() + timedelta(minutes=30))
        with self.assertRaises(ValidationError):
            require_installation_readiness(self.subscription)
        session = RadiusSession.objects.create(router=router, username=self.subscription.access_username, session_id="test")
        self.assertTrue(require_installation_readiness(self.subscription)["ready"])
        RadiusSession.objects.filter(pk=session.pk).update(updated_at=timezone.now() - timedelta(minutes=16))
        with self.assertRaises(ValidationError):
            require_installation_readiness(self.subscription)

    def test_overlapping_period_for_same_outage_cannot_double_credit(self):
        outage = Outage.objects.create(organization=self.org, title="Falla", started_at=self.start, ended_at=self.start + timedelta(days=1), provider_attributable=True, attribution_evidence="Falla propia")
        outage.subscriptions.add(self.subscription)
        record_outage_credit(outage, self.subscription, self.start, self.end, self.staff)
        with self.assertRaises(ValidationError):
            record_outage_credit(outage, self.subscription, self.start, self.start + timedelta(days=10), self.staff)

    def test_site_not_applicable_requires_basis(self):
        site = Site(organization=self.org, name="Sitio", address="Centro", permit_status="not_required")
        with self.assertRaises(ValidationError):
            site.full_clean()

    def test_operations_pages_require_staff(self):
        user = get_user_model().objects.create_user(username="customer")
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse("operations:index")).status_code, 403)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("operations:index")).status_code, 200)
        self.assertEqual(self.client.get(reverse("compliance:index")).status_code, 403)
