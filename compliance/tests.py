from datetime import date, timedelta
from decimal import Decimal
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from core.models import AuditEvent, Customer, Organization, Plan, Subscription
from .models import ARCOExtension, ARCORequest, BusinessHoliday, CancellationRequest, DocumentVersion, LegalRequirement, PlanRegistration, RegulatoryNotice, RetentionDisposal, RetentionHold, RetentionPolicy
from .services import (REQUIRED_PRODUCTION_CODES, add_business_days, cancel_subscription, can_dispose, complete_arco, create_arco_request, extend_arco, legal_readiness, record_consent, release_retention_hold, require_customer_documents, require_legal_readiness, require_plan_registration, respond_arco, verify_arco_identity, withdraw_consent)
from .workflows import (REDACTED, accept_regulatory_notice, confirm_notice_delivery, execute_retention_disposal, preview_retention_disposal, publish_regulatory_notice, regulatory_notice_ready)


class ComplianceTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username="compliance", is_staff=True)
        self.staff.groups.add(Group.objects.get_or_create(name="Cumplimiento")[0])
        self.user = get_user_model().objects.create_user(username="owner")
        self.org = Organization.objects.create(name="Operador", demo_mode=False)
        self.customer = Customer.objects.create(organization=self.org, code="COMP1", name="Cliente", address="Cuauhtémoc", user=self.user)
        self.plan = Plan.objects.create(organization=self.org, name="Plan", download_mbps=10, upload_mbps=5, price_mxn=Decimal("200"))
        self.subscription = Subscription.objects.create(customer=self.customer, plan=self.plan, access_username="comp1", status="active")

    def test_empty_registry_blocks_production_but_demo_remains_explicit(self):
        self.assertFalse(legal_readiness(self.org)["ready"])
        with self.assertRaises(ValidationError):
            require_legal_readiness(self.org)
        self.org.demo_mode = True
        result = require_legal_readiness(self.org)
        self.assertFalse(result["ready"])
        self.assertTrue(result["demo_mode"])

    def test_complete_reviewed_registry_and_expiry(self):
        for code, title in REQUIRED_PRODUCTION_CODES.items():
            LegalRequirement.objects.create(organization=self.org, code=code, title=title, source_url="https://www.diputados.gob.mx", legal_reference="Revisión aplicable", evidence="Expediente y folio verificados", status="approved", reviewed_by=self.staff, reviewed_at=timezone.now())
        self.assertTrue(legal_readiness(self.org)["ready"])
        item = LegalRequirement.objects.first()
        item.expires_on = timezone.localdate() - timedelta(days=1)
        item.save()
        self.assertFalse(legal_readiness(self.org)["ready"])

    def test_claimed_approval_without_evidence_is_not_ready(self):
        for code, title in REQUIRED_PRODUCTION_CODES.items():
            LegalRequirement.objects.create(organization=self.org, code=code, title=title, source_url="https://www.diputados.gob.mx", legal_reference="Ley", status="approved")
        self.assertFalse(legal_readiness(self.org)["ready"])

    def test_activation_requires_customer_contract_and_privacy_evidence(self):
        PlanRegistration.objects.create(plan=self.plan, tariff_reference="Tarifa de prueba", registered_on=timezone.localdate(), effective_on=timezone.localdate(), source_url="https://portal.crt.gob.mx/", evidence="Expediente de prueba", approved=True, reviewed_by=self.staff, reviewed_at=timezone.now(), plan_snapshot=PlanRegistration.snapshot(self.plan))
        with self.assertRaises(ValidationError):
            require_customer_documents(self.subscription)
        contract = DocumentVersion.objects.create(organization=self.org, kind="contract", version="1", title="Contrato", content="Contrato de prueba", status="approved", approved_by=self.staff, registration_reference="Registro de prueba")
        privacy = DocumentVersion.objects.create(organization=self.org, kind="privacy", version="1", title="Aviso", content="Aviso de prueba", status="approved", approved_by=self.staff)
        record_consent(self.customer, contract, "contract", "portal", "Aceptación expresa", self.user)
        with self.assertRaises(ValidationError):
            require_customer_documents(self.subscription)
        record_consent(self.customer, privacy, "privacy_notice", "portal", "Recepción confirmada", self.user)
        self.assertTrue(require_customer_documents(self.subscription)["ready"])

    def test_tariff_registration_must_match_exact_plan_and_be_effective(self):
        with self.assertRaises(ValidationError):
            require_plan_registration(self.plan)
        record = PlanRegistration.objects.create(plan=self.plan, tariff_reference="Tarifa de prueba", registered_on=timezone.localdate(), effective_on=timezone.localdate(), source_url="https://portal.crt.gob.mx/", evidence="Expediente de prueba", approved=True, reviewed_by=self.staff, reviewed_at=timezone.now(), plan_snapshot=PlanRegistration.snapshot(self.plan))
        self.assertTrue(require_plan_registration(self.plan)["ready"])
        self.plan.price_mxn += Decimal("1")
        self.plan.save()
        with self.assertRaises(ValidationError):
            require_plan_registration(self.plan)
        record.evidence = "Evidencia sustituida sin revocar revisión"
        with self.assertRaises(ValidationError):
            record.save()

    def test_business_days_exclude_mexican_holidays_and_use_overrides(self):
        self.assertEqual(add_business_days(date(2026, 9, 15), 1, self.org), date(2026, 9, 17))
        BusinessHoliday.objects.create(organization=self.org, date=date(2026, 9, 17), name="Suspensión de labores")
        self.assertEqual(add_business_days(date(2026, 9, 15), 1, self.org), date(2026, 9, 18))
        BusinessHoliday.objects.create(organization=self.org, date=date(2026, 9, 19), name="Sábado habilitado", is_working_day=True)
        self.assertEqual(add_business_days(date(2026, 9, 18), 1, self.org), date(2026, 9, 19))

    def test_arco_identity_deadlines_and_single_extension(self):
        record = create_arco_request(self.customer, "access", "Copia de datos", self.user)
        original_deadline = record.response_due_on
        with self.assertRaises(ValidationError):
            respond_arco(record.pk, self.staff, "Procede", True)
        verify_arco_identity(record.pk, self.staff, "Identidad confirmada en sesión presencial, folio V-1")
        record.refresh_from_db()
        self.assertEqual(record.response_due_on, original_deadline)
        extension = extend_arco(record.pk, self.staff, "response", "Localizar documentos archivados", "Aviso entregado al titular")
        self.assertEqual(extension.extended_due_on, add_business_days(original_deadline, 20, self.org))
        with self.assertRaises(ValidationError):
            extend_arco(record.pk, self.staff, "response", "Más tiempo", "Aviso")
        record = respond_arco(record.pk, self.staff, "Se concede acceso a los datos del titular", True)
        self.assertEqual(record.implementation_due_on, add_business_days(record.decision_sent_on, 15, self.org))
        complete_arco(record.pk, self.staff, "Copia entregada mediante canal autenticado")
        record.refresh_from_db()
        self.assertEqual(record.status, "completed")
        self.assertEqual(ARCOExtension.objects.count(), 1)

    def test_consent_snapshot_is_preserved_and_delivery_is_not_marketing(self):
        document = DocumentVersion.objects.create(organization=self.org, kind="privacy", version="1", title="Aviso", content="Aviso original", status="approved", approved_by=self.staff)
        record = record_consent(self.customer, document, "privacy_notice", "portal", "Aviso mostrado y entrega confirmada", self.user)
        self.assertEqual(record.content_snapshot, "Aviso original")
        self.assertEqual(record.content_hash, document.content_hash)
        with self.assertRaises(ValidationError):
            withdraw_consent(record.pk, self.user)
        document.content = "Aviso cambiado"
        with self.assertRaises(ValidationError):
            document.full_clean()
        with self.assertRaises(ValidationError):
            document.save()
        record.evidence = "Intento de sustituir constancia"
        with self.assertRaises(ValidationError):
            record.save()
        marketing = record_consent(self.customer, record.document, "marketing", "portal", "Casilla publicitaria marcada expresamente", self.user)
        withdraw_consent(marketing.pk, self.user)
        marketing.refresh_from_db()
        self.assertIsNotNone(marketing.withdrawn_at)

    def test_foreign_or_draft_document_rejected(self):
        org = Organization.objects.create(name="Otro")
        document = DocumentVersion.objects.create(organization=org, kind="privacy", version="1", title="Aviso", content="Contenido", status="approved", approved_by=self.staff)
        with self.assertRaises(ValidationError):
            record_consent(self.customer, document, "privacy_notice", "portal", "Entrega", self.user)

    def test_cancellation_is_owned_immediate_audited_and_idempotent(self):
        other = get_user_model().objects.create_user(username="stranger")
        with self.assertRaises(PermissionDenied):
            cancel_subscription(self.subscription.pk, other, "portal")
        receipt = cancel_subscription(self.subscription.pk, self.user, "portal", "No requiere servicio")
        repeated = cancel_subscription(self.subscription.pk, self.user, "portal")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "cancelled")
        self.assertEqual(receipt.pk, repeated.pk)
        self.assertTrue(receipt.network_disconnect_pending)
        self.assertEqual(CancellationRequest.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="subscription.cancelled").count(), 1)

    def test_retention_hold_blocks_disposal_and_release_is_audited(self):
        RetentionPolicy.objects.create(organization=self.org, category="contracts", lawful_basis="Responsabilidades contractuales", retention_days=365, disposal_method="Borrado verificado", approved=True, reviewed_on=timezone.localdate())
        hold = RetentionHold.objects.create(customer=self.customer, category="*", reason="Controversia pendiente")
        self.assertFalse(can_dispose(self.customer, "contracts"))
        with self.assertRaises(ValidationError):
            release_retention_hold(hold.pk, self.staff, "")
        release_retention_hold(hold.pk, self.staff, "Resolución firme documentada")
        self.assertEqual(AuditEvent.objects.filter(action="retention.hold_released").count(), 1)
        today = timezone.localdate()
        self.assertFalse(can_dispose(self.customer, "contracts"))
        self.assertFalse(can_dispose(self.customer, "contracts", today - timedelta(days=364), True))
        self.assertFalse(can_dispose(self.customer, "contracts", today - timedelta(days=365), False))
        self.assertTrue(can_dispose(self.customer, "contracts", today - timedelta(days=365), True))
        RetentionPolicy.objects.create(organization=self.org, category="contracts", lawful_basis="Otra obligación aplicable", retention_days=730, disposal_method="Borrado verificado", approved=True, reviewed_on=today)
        self.assertFalse(can_dispose(self.customer, "contracts", today - timedelta(days=365), True))

    def test_other_customer_cannot_withdraw_consent_or_create_arco(self):
        document = DocumentVersion.objects.create(organization=self.org, kind="privacy", version="1", title="Aviso", content="Contenido", status="approved", approved_by=self.staff)
        record = record_consent(self.customer, document, "marketing", "portal", "Aceptación expresa", self.user)
        stranger = get_user_model().objects.create_user(username="outsider")
        with self.assertRaises(PermissionDenied):
            withdraw_consent(record.pk, stranger)
        with self.assertRaises(PermissionDenied):
            create_arco_request(self.customer, "access", "Datos ajenos", stranger)

    def test_export_requires_staff_and_neutralizes_csv_formula(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("compliance:register_export")).status_code, 403)
        LegalRequirement.objects.create(organization=self.org, code="export", title="=1+1", source_url="https://www.diputados.gob.mx", legal_reference="Ley")
        self.client.force_login(self.staff)
        response = self.client.get(reverse("compliance:register_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("'=1+1", response.content.decode())


class RegulatoryWorkflowTests(TestCase):
    def setUp(self):
        self.staff = get_user_model().objects.create_user(username="reviewer", is_staff=True)
        self.staff.groups.add(Group.objects.get_or_create(name="Cumplimiento")[0])
        self.user = get_user_model().objects.create_user(username="subscriber")
        self.org = Organization.objects.create(name="Prueba", demo_mode=False)
        self.customer = Customer.objects.create(organization=self.org, code="WF1", name="Cliente", address="Centro", user=self.user)
        plan = Plan.objects.create(organization=self.org, name="Plan", download_mbps=20, upload_mbps=5, price_mxn="199")
        self.subscription = Subscription.objects.create(customer=self.customer, plan=plan, access_username="workflow", status="active")

    def notice(self, days=30, kind="contract_change"):
        return RegulatoryNotice.objects.create(subscription=self.subscription, kind=kind, effective_on=timezone.localdate() + timedelta(days=days), title="Aviso de condiciones", body="Descripción completa del cambio propuesto", created_by=self.staff, renewal_amount_mxn="199" if kind == "automatic_renewal" else None, renewal_frequency="mensual" if kind == "automatic_renewal" else "")

    def policy(self, category):
        return RetentionPolicy.objects.create(organization=self.org, category=category, lawful_basis="Finalidad concluida y plazo documentado de prueba", retention_days=30, disposal_method="Supresión de contenido", approved=True, reviewed_on=timezone.localdate())

    def closed_ticket(self):
        from operations.models import Ticket
        return Ticket.objects.create(customer=self.customer, subject="Texto personal", description="Descripción personal", status="resolved", resolution="Respuesta personal", resolved_at=timezone.now() - timedelta(days=40))

    def test_contract_notice_needs_30_days_delivery_and_acceptance(self):
        from core.models import Notification
        with self.assertRaises(ValidationError):
            publish_regulatory_notice(self.notice(days=29).pk, self.staff)
        notice = self.notice()
        publish_regulatory_notice(notice.pk, self.staff)
        publish_regulatory_notice(notice.pk, self.staff)
        self.assertEqual(Notification.objects.count(), 1)
        notice.refresh_from_db()
        self.assertFalse(regulatory_notice_ready(notice, notice.effective_on))
        confirm_notice_delivery(notice.pk, self.staff, "portal", "Recepción autenticada confirmada")
        notice.refresh_from_db()
        self.assertFalse(regulatory_notice_ready(notice, notice.effective_on))
        accept_regulatory_notice(notice.pk, self.user, "Aceptación expresa en portal")
        notice.refresh_from_db()
        self.assertFalse(regulatory_notice_ready(notice))
        self.assertTrue(regulatory_notice_ready(notice, notice.effective_on))
        notice.body = "Intento de modificar aviso ya publicado"
        with self.assertRaises(ValidationError):
            notice.save()

    def test_renewal_5_day_notice_discloses_amount_and_frequency(self):
        with self.assertRaises(ValidationError):
            publish_regulatory_notice(self.notice(days=4, kind="automatic_renewal").pk, self.staff)
        notice = publish_regulatory_notice(self.notice(days=5, kind="automatic_renewal").pk, self.staff)
        self.assertIn("199.00 MXN", notice.notification.body)
        self.assertIn("mensual", notice.notification.body)
        self.assertIn("cancelar", notice.notification.body)
        stranger = get_user_model().objects.create_user(username="stranger")
        with self.assertRaises(PermissionDenied):
            accept_regulatory_notice(notice.pk, stranger, "No es mi servicio")

    def test_retention_execution_requires_review_and_is_idempotent(self):
        self.policy("support_ticket_content")
        ticket = self.closed_ticket()
        preview = preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        with self.assertRaises(ValidationError):
            execute_retention_disposal(preview.pk, self.staff, False, "Copias revisadas")
        ticket.refresh_from_db()
        self.assertEqual(ticket.description, "Descripción personal")
        execute_retention_disposal(preview.pk, self.staff, True, "Copias externas identificadas y plazo de respaldo documentado")
        execute_retention_disposal(preview.pk, self.staff, True, "Segundo intento")
        ticket.refresh_from_db()
        self.assertEqual(ticket.description, REDACTED)
        self.assertEqual(ticket.subject, "Ticket archivado")
        self.assertEqual(AuditEvent.objects.filter(action="retention.content_redacted").count(), 1)
        self.assertTrue(Customer.objects.filter(pk=self.customer.pk).exists())

    def test_hold_added_after_preview_blocks_execution(self):
        self.policy("support_ticket_content")
        ticket = self.closed_ticket()
        preview = preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        RetentionHold.objects.create(customer=self.customer, category="*", reason="Requerimiento sobrevenido")
        with self.assertRaises(ValidationError):
            execute_retention_disposal(preview.pk, self.staff, True, "Copias revisadas")
        ticket.refresh_from_db()
        self.assertEqual(ticket.description, "Descripción personal")

    def test_record_change_after_preview_requires_new_review(self):
        self.policy("support_ticket_content")
        ticket = self.closed_ticket()
        preview = preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        ticket.resolution = "Nueva evidencia agregada"
        ticket.save(update_fields=["resolution"])
        with self.assertRaises(ValidationError):
            execute_retention_disposal(preview.pk, self.staff, True, "Copias revisadas")

    def test_expired_preview_does_not_redact(self):
        self.policy("support_ticket_content")
        ticket = self.closed_ticket()
        preview = preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        RetentionDisposal.objects.filter(pk=preview.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(ValidationError):
            execute_retention_disposal(preview.pk, self.staff, True, "Copias revisadas")
        ticket.refresh_from_db()
        self.assertEqual(ticket.description, "Descripción personal")

    def test_active_dispute_and_unreviewed_policy_block_retention(self):
        from operations.models import Ticket
        policy = self.policy("support_ticket_content")
        self.closed_ticket()
        policy.approved = False
        policy.save()
        with self.assertRaises(ValidationError):
            preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        policy.approved = True
        policy.save()
        Ticket.objects.create(customer=self.customer, kind="billing", subject="Aclaración", description="Pendiente")
        with self.assertRaises(ValidationError):
            preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        with self.assertRaises(PermissionDenied):
            preview_retention_disposal(self.customer, "support_ticket_content", self.user)

    def test_completed_arco_redaction_keeps_deadline_metadata(self):
        self.policy("arco_request_content")
        completed_on = timezone.localdate() - timedelta(days=40)
        record = ARCORequest.objects.create(customer=self.customer, request_type="access", description="Información personal", status="completed", completed_on=completed_on, decision="Información entregada", identity_evidence="Referencia de identidad")
        preview = preview_retention_disposal(self.customer, "arco_request_content", self.staff)
        execute_retention_disposal(preview.pk, self.staff, True, "Procedimiento de respaldos documentado")
        record.refresh_from_db()
        self.assertEqual(record.description, REDACTED)
        self.assertEqual(record.identity_evidence, REDACTED)
        self.assertEqual(record.completed_on, completed_on)

    def test_staff_workflow_pages_render(self):
        self.client.force_login(self.staff)
        notice = self.notice()
        self.policy("support_ticket_content")
        self.closed_ticket()
        preview = preview_retention_disposal(self.customer, "support_ticket_content", self.staff)
        for url in [reverse("compliance:index"), reverse("compliance:notice_detail", args=[notice.pk]), reverse("compliance:disposal_detail", args=[preview.pk])]:
            self.assertEqual(self.client.get(url).status_code, 200)

    def test_staff_same_channel_cancellation_emits_folio(self):
        self.client.force_login(self.staff)
        response = self.client.post(reverse("compliance:cancellation_create"), {"subscription": self.subscription.pk, "channel": "teléfono", "reason": "Solicitud del titular", "confirmed": "on"})
        self.assertEqual(response.status_code, 302)
        record = CancellationRequest.objects.get(subscription=self.subscription)
        self.assertEqual(record.channel, "teléfono")
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status, "cancelled")

    def test_portal_notice_receipt_does_not_imply_acceptance_and_is_owned(self):
        notice = publish_regulatory_notice(self.notice().pk, self.staff)
        other_user = get_user_model().objects.create_user(username="other-owner")
        other_customer = Customer.objects.create(organization=self.org, code="WF2", name="Otro", address="Otro", user=other_user)
        other_subscription = Subscription.objects.create(customer=other_customer, plan=self.subscription.plan, access_username="other")
        other_notice = RegulatoryNotice.objects.create(subscription=other_subscription, kind="contract_change", effective_on=timezone.localdate() + timedelta(days=30), title="Aviso ajeno", body="Información de otro cliente", created_by=self.staff)
        publish_regulatory_notice(other_notice.pk, self.staff)
        self.client.force_login(self.user)
        listing = self.client.get(reverse("core:portal_notices"))
        self.assertContains(listing, "Aviso de condiciones")
        self.assertNotContains(listing, "Aviso ajeno")
        own_url = reverse("core:portal_notice_respond", args=[notice.pk])
        self.client.get(own_url)
        notice.refresh_from_db()
        self.assertIsNone(notice.delivered_on)
        self.assertEqual(self.client.post(reverse("core:portal_notice_respond", args=[other_notice.pk]), {"received": "on", "accepted": "on"}).status_code, 404)
        self.client.post(own_url, {"received": "on"})
        notice.refresh_from_db()
        self.assertIsNotNone(notice.delivered_on)
        self.assertIsNone(notice.accepted_at)
        self.client.post(own_url, {"received": "on", "accepted": "on"})
        notice.refresh_from_db()
        self.assertIsNotNone(notice.accepted_at)
