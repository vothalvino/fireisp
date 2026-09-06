from decimal import Decimal, ROUND_HALF_UP
from datetime import timedelta
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Equipment, Outage, Ticket, WorkOrder, OutageCredit


def require_installation_readiness(subscription):
    """Gate the first production activation on field evidence and recent actual access."""
    if subscription.customer.organization.demo_mode:
        return {"ready": False, "demo_mode": True}
    work = subscription.work_orders.filter(kind="installation", status="completed", completed_at__isnull=False).exclude(completion_evidence="").first()
    if not work or not work.completion_evidence.strip():
        raise ValidationError("Completa una instalación con pruebas y aceptación del cliente antes de activar.")
    items = list(Equipment.objects.select_related("sector__site").filter(subscription=subscription, role="cpe", status="installed"))
    if not items:
        raise ValidationError("Asigna el CPE instalado y documenta su cumplimiento de radio.")
    for item in items:
        item.full_clean()
        if not item.homologation_verified or not item.sector_id or not item.regulatory_profile.strip() or item.eirp_dbm is None or item.allowed_eirp_dbm is None:
            raise ValidationError("Falta verificar certificado, perfil de radio, sector y PIRE del CPE.")
        sector, site = item.sector, item.sector.site
        sector.full_clean()
        site.full_clean()
        if not sector.frequency_mhz or not sector.channel_width_mhz or not sector.regulatory_basis.strip():
            raise ValidationError("Documenta la banda, ancho de canal y disposición aplicable al sector.")
        if site.permit_status == "unreviewed" or not site.owner_permission.strip() or (site.permit_expires_on and site.permit_expires_on < timezone.localdate()):
            raise ValidationError("El sitio requiere permiso del propietario y expediente vigente de autorizaciones.")
    from network.models import RadiusCredential, RadiusSession
    credential = RadiusCredential.objects.select_related("router").filter(subscription=subscription, username=subscription.access_username, enabled=True, is_lab=False, router__is_lab=False, router__organization_id=subscription.customer.organization_id).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now())).first()
    max_age = timedelta(minutes=getattr(settings, "INSTALLATION_SESSION_MAX_AGE_MINUTES", 15))
    if not credential or not RadiusSession.objects.filter(router=credential.router, username=credential.username, stopped_at__isnull=True, updated_at__gte=timezone.now() - max_age).exists():
        raise ValidationError("Falta una sesión RADIUS real y reciente del cliente. Realiza la prueba temporal de instalación.")
    return {"ready": True, "demo_mode": False}


def has_billing_dispute_hold(subscription):
    """Account-wide billing complaints protect every service of that account."""
    return Ticket.objects.filter(customer_id=subscription.customer_id, kind="billing").exclude(status="resolved").filter(Q(subscription__isnull=True) | Q(subscription_id=subscription.pk)).exists()


@transaction.atomic
def resolve_ticket(ticket_id, actor, resolution):
    from core.services import audit
    ticket = Ticket.objects.select_for_update().get(pk=ticket_id)
    if ticket.status == "resolved":
        return ticket
    ticket.resolution = resolution.strip()
    ticket.status = "resolved"
    ticket.resolved_at = timezone.now()
    ticket.full_clean()
    ticket.save(update_fields=["resolution", "status", "resolved_at"])
    audit(actor, "ticket.resolved", ticket, {"folio": ticket.folio})
    return ticket


@transaction.atomic
def complete_work_order(work_order_id, actor, evidence=None):
    from core.services import activate_subscription, audit
    work = WorkOrder.objects.select_for_update(of=("self",)).select_related("subscription__customer__organization").get(pk=work_order_id)
    if work.status == "completed":
        return work
    if work.status == "cancelled":
        raise ValidationError("La orden fue cancelada.")
    if evidence is not None:
        work.completion_evidence = evidence.strip()
    if not work.completion_evidence.strip():
        raise ValidationError("Registra las pruebas y la aceptación antes de completar la orden.")
    work.status = "completed"
    work.completed_at = timezone.now()
    work.save(update_fields=["completion_evidence", "status", "completed_at"])
    if work.kind == "installation":
        require_installation_readiness(work.subscription)
        activate_subscription(work.subscription_id, actor)
    audit(actor, "work_order.completed", work)
    return work


def calculate_outage_credit(price, start, end, period_start, period_end):
    """Use the actual charged period, never assume a 30-day month."""
    price = Decimal(str(price))
    if price < 0 or period_end <= period_start or end <= start:
        raise ValidationError("Importe o intervalo de compensación inválido.")
    affected_start, affected_end = max(start, period_start), min(end, period_end)
    duration = Decimal(str((period_end - period_start).total_seconds()))
    affected = Decimal(str(max(0, (affected_end - affected_start).total_seconds())))
    proportional = (price * affected / duration).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    bonus = (proportional * Decimal("0.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"proportional": proportional, "bonus": bonus, "total": proportional + bonus}


@transaction.atomic
def record_outage_credit(outage, subscription, period_start, period_end, actor, price=None):
    from core.services import audit
    outage = Outage.objects.select_for_update().get(pk=outage.pk)
    outage.full_clean()
    if not outage.ended_at or not outage.provider_attributable:
        raise ValidationError("La falla requiere cierre y atribución documentada antes de calcular compensaciones.")
    if subscription.customer.organization_id != outage.organization_id or not outage.subscriptions.filter(pk=subscription.pk).exists():
        raise ValidationError("El servicio no está incluido entre los afectados de esta falla.")
    amounts = calculate_outage_credit(price if price is not None else subscription.plan.price_mxn, outage.started_at, outage.ended_at, period_start, period_end)
    overlaps = OutageCredit.objects.filter(outage=outage, subscription=subscription, period_start__lt=period_end, period_end__gt=period_start).exclude(period_start=period_start, period_end=period_end)
    if overlaps.exists():
        raise ValidationError("Ya existe una compensación para un periodo que se solapa con éste.")
    record, created = OutageCredit.objects.get_or_create(outage=outage, subscription=subscription, period_start=period_start, period_end=period_end, defaults={"proportional_amount": amounts["proportional"], "bonus_amount": amounts["bonus"]})
    if created:
        audit(actor, "outage.credit_calculated", record, {"total": str(record.total), "ledger_applied": False})
    return record
