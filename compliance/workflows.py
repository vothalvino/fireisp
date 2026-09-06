"""Reviewable regulatory notices and narrowly scoped retention execution."""
import hashlib
import json
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import ARCORequest, RegulatoryNotice, RetentionDisposal

REDACTED = "[Contenido suprimido conforme a política de conservación]"


def _staff(actor):
    if not actor or not actor.is_authenticated or not actor.is_staff:
        raise PermissionDenied


@transaction.atomic
def publish_regulatory_notice(notice_id, actor):
    from core.models import Notification
    from core.services import audit
    _staff(actor)
    notice = RegulatoryNotice.objects.select_for_update(of=("self",)).select_related("subscription__customer", "document").get(pk=notice_id)
    if notice.published_at:
        return notice
    notice.full_clean()
    if notice.subscription.status == "cancelled":
        raise ValidationError("El servicio está cancelado.")
    if timezone.localdate() > notice.notify_by:
        raise ValidationError(f"El aviso requiere al menos {notice.minimum_notice_days} días naturales de anticipación.")
    if notice.document and (notice.document.status != "approved" or notice.document.effective_on > notice.effective_on):
        raise ValidationError("La versión propuesta debe estar aprobada y ser aplicable a la fecha del cambio.")
    body = notice.body
    if notice.kind == "automatic_renewal":
        body += f"\n\nImporte con impuestos: ${notice.renewal_amount_mxn:.2f} MXN. Periodicidad: {notice.renewal_frequency}."
    body += f"\n\nFecha prevista: {notice.effective_on:%d/%m/%Y}. Puedes cancelar este servicio desde Mi servicio / Cancelar, o por el canal en que lo contrataste."
    notice.notification = Notification.objects.create(customer=notice.subscription.customer, title=notice.title, body=body)
    notice.published_at = timezone.now()
    notice.save(update_fields=["notification", "published_at"])
    audit(actor, "regulatory_notice.published", notice.pk, {"kind": notice.kind, "effective_on": str(notice.effective_on), "portal_available": True, "delivery_confirmed": False})
    return notice


@transaction.atomic
def confirm_notice_delivery(notice_id, actor, channel, evidence, delivered_on=None):
    from core.services import audit
    _staff(actor)
    notice = RegulatoryNotice.objects.select_for_update().get(pk=notice_id)
    if not notice.published_at or not channel.strip() or not evidence.strip():
        raise ValidationError("Publica el aviso y registra canal y evidencia de entrega.")
    delivered_on = delivered_on or timezone.localdate()
    if delivered_on < timezone.localtime(notice.published_at).date() or delivered_on > timezone.localdate():
        raise ValidationError("La entrega no puede anteceder a la publicación ni tener fecha futura.")
    if notice.delivered_on:
        if (notice.delivered_on, notice.delivery_channel, notice.delivery_evidence) != (delivered_on, channel.strip(), evidence.strip()):
            raise ValidationError("La constancia de entrega registrada es inmutable.")
        return notice
    notice.delivered_on = delivered_on
    notice.delivery_channel = channel.strip()
    notice.delivery_evidence = evidence.strip()
    notice.save(update_fields=["delivered_on", "delivery_channel", "delivery_evidence"])
    audit(actor, "regulatory_notice.delivery_confirmed", notice.pk, {"channel": channel, "timely": delivered_on <= notice.notify_by})
    return notice


@transaction.atomic
def accept_regulatory_notice(notice_id, actor, evidence):
    from core.services import audit
    notice = RegulatoryNotice.objects.select_for_update(of=("self",)).select_related("subscription__customer").get(pk=notice_id)
    if not actor or not actor.is_authenticated or (not actor.is_staff and notice.subscription.customer.user_id != actor.pk):
        raise PermissionDenied
    if not notice.published_at or not evidence.strip():
        raise ValidationError("Se necesita aviso publicado y evidencia de aceptación expresa.")
    if notice.accepted_at:
        return notice
    notice.accepted_at = timezone.now()
    notice.acceptance_evidence = evidence.strip()
    notice.save(update_fields=["accepted_at", "acceptance_evidence"])
    audit(actor, "regulatory_notice.accepted", notice.pk)
    return notice


@transaction.atomic
def acknowledge_notice_receipt(notice_id, actor):
    from core.services import audit
    notice = RegulatoryNotice.objects.select_for_update(of=("self",)).select_related("subscription__customer").get(pk=notice_id)
    if not actor or not actor.is_authenticated or notice.subscription.customer.user_id != actor.pk:
        raise PermissionDenied
    if not notice.published_at:
        raise ValidationError("El aviso todavía no está publicado.")
    if not notice.delivered_on:
        notice.delivered_on = timezone.localdate()
        notice.delivery_channel = "portal"
        notice.delivery_evidence = f"El titular confirmó recepción en sesión autenticada; usuario {actor.pk}."
        notice.save(update_fields=["delivered_on", "delivery_channel", "delivery_evidence"])
        if notice.notification_id:
            from core.models import Notification
            Notification.objects.filter(pk=notice.notification_id, read_at__isnull=True).update(read_at=timezone.now())
        audit(actor, "regulatory_notice.receipt_acknowledged", notice.pk, {"timely": notice.delivered_on <= notice.notify_by})
    return notice


def regulatory_notice_ready(notice, as_of=None):
    """An available portal notification alone never proves timely delivery or consent."""
    as_of = as_of or timezone.localdate()
    return bool(notice.published_at and notice.delivered_on and notice.delivered_on <= notice.notify_by and (not notice.requires_consent or notice.accepted_at) and notice.effective_on <= as_of and notice.subscription.status != "cancelled")


def _content_rows(customer, category, lock=False):
    if category == "support_ticket_content":
        from operations.models import Ticket
        rows = Ticket.objects.filter(customer=customer, status="resolved", resolved_at__isnull=False).exclude(description=REDACTED).order_by("pk")
    elif category == "arco_request_content":
        rows = ARCORequest.objects.filter(customer=customer, status="completed", completed_on__isnull=False).exclude(description=REDACTED).order_by("pk")
    else:
        raise ValidationError("Esta categoría requiere un procedimiento de eliminación independiente.")
    return list(rows.select_for_update() if lock else rows)


def _eligible_rows(customer, category, lock=False):
    from operations.models import Ticket
    from .services import can_dispose
    if Ticket.objects.filter(customer=customer, kind="billing").exclude(status="resolved").exists():
        raise ValidationError("Hay una aclaración de facturación pendiente; conserva el expediente.")
    rows = _content_rows(customer, category, lock)
    return [row for row in rows if can_dispose(customer, category, row.resolved_at if category == "support_ticket_content" else row.completed_on, purpose_ended=True)]


def _digest(rows, category):
    snapshot = []
    for row in rows:
        if category == "support_ticket_content":
            values = [row.pk, str(row.resolved_at), row.subject, row.description, row.resolution]
        else:
            values = [row.pk, str(row.completed_on), row.description, row.identity_evidence, row.decision, row.implementation_evidence, list(row.extensions.order_by("pk").values_list("pk", "reason", "notification_evidence"))]
        snapshot.append(values)
    return hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _lock_retention_context(customer, category):
    from core.models import Customer, Organization
    from .models import RetentionHold, RetentionPolicy
    Organization.objects.select_for_update().get(pk=customer.organization_id)
    Customer.objects.select_for_update().get(pk=customer.pk)
    list(RetentionHold.objects.select_for_update().filter(customer=customer).values_list("pk", flat=True))
    list(RetentionPolicy.objects.select_for_update().filter(organization_id=customer.organization_id, category=category).values_list("pk", flat=True))


@transaction.atomic
def preview_retention_disposal(customer, category, actor):
    from core.services import audit
    _staff(actor)
    _lock_retention_context(customer, category)
    rows = _eligible_rows(customer, category, lock=True)
    if not rows:
        raise ValidationError("No hay registros cerrados con finalidad concluida y plazo revisado vencido, libres de conservación.")
    preview = RetentionDisposal.objects.create(customer=customer, category=category, record_ids=[row.pk for row in rows], snapshot_hash=_digest(rows, category), expires_at=timezone.now() + timedelta(hours=24), created_by=actor)
    audit(actor, "retention.disposal_previewed", preview.pk, {"category": category, "count": len(rows)})
    return preview


@transaction.atomic
def execute_retention_disposal(preview_id, actor, confirmed, external_copies_evidence):
    from core.services import audit
    _staff(actor)
    if confirmed is not True or not external_copies_evidence.strip():
        raise ValidationError("Confirma la lista revisada y documenta el tratamiento de copias externas y respaldos.")
    preview = RetentionDisposal.objects.select_for_update(of=("self",)).select_related("customer").get(pk=preview_id)
    if preview.performed_at:
        return preview
    if preview.expires_at <= timezone.now():
        raise ValidationError("La revisión caducó. Genera una nueva vista previa.")
    _lock_retention_context(preview.customer, preview.category)
    rows = [row for row in _eligible_rows(preview.customer, preview.category, lock=True) if row.pk in preview.record_ids]
    if [row.pk for row in rows] != preview.record_ids or _digest(rows, preview.category) != preview.snapshot_hash:
        raise ValidationError("El expediente, plazo o conservación cambió. Genera una nueva vista previa.")
    if preview.category == "support_ticket_content":
        from operations.models import Ticket
        Ticket.objects.filter(pk__in=preview.record_ids).update(subject="Ticket archivado", description=REDACTED, resolution=REDACTED)
    else:
        from .models import ARCOExtension
        ARCORequest.objects.filter(pk__in=preview.record_ids).update(description=REDACTED, identity_evidence=REDACTED, decision=REDACTED, implementation_evidence=REDACTED)
        ARCOExtension.objects.filter(request_id__in=preview.record_ids).update(reason=REDACTED, notification_evidence=REDACTED)
    preview.performed_at = timezone.now()
    preview.performed_by = actor
    preview.external_copies_evidence = external_copies_evidence.strip()
    preview.save(update_fields=["performed_at", "performed_by", "external_copies_evidence"])
    audit(actor, "retention.content_redacted", preview.pk, {"category": preview.category, "count": len(rows), "scope": "application_content_only"})
    return preview
