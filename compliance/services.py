import calendar
from datetime import date, timedelta
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from .models import (ARCOExtension, ARCORequest, BusinessHoliday, CancellationRequest,
                     Consent, DocumentVersion, LegalRequirement, PlanRegistration, RetentionHold, RetentionPolicy)

REQUIRED_PRODUCTION_CODES = {
    "service_title": "Título habilitante para prestar el servicio",
    "tariff_registration": "Tarifas registradas",
    "contract_registration": "Contrato PROFECO/RPC",
    "privacy_notice": "Aviso y procedimiento de privacidad",
    "radio_compliance": "Equipos y bandas de radio verificados",
    "site_permissions": "Expedientes de sitios verificados",
}


def legal_readiness(organization):
    requirements = {r.code: r for r in LegalRequirement.objects.filter(organization=organization)}
    today = timezone.localdate()
    missing = []
    for code, label in REQUIRED_PRODUCTION_CODES.items():
        record = requirements.get(code)
        if not record:
            missing.append(label)
    for record in requirements.values():
        if not record.production_required and record.code not in REQUIRED_PRODUCTION_CODES:
            continue
        valid = bool(record.evidence.strip()) and record.status == "approved" and bool(record.reviewed_by_id and record.reviewed_at)
        if record.code in {"service_title", "tariff_registration", "contract_registration", "privacy_notice"} and not record.applicable:
            valid = False
        if record.expires_on and record.expires_on < today:
            valid = False
        if not valid:
            missing.append(record.title)
    return {"ready": not missing, "missing": missing, "demo_mode": organization.demo_mode}


def require_legal_readiness(organization):
    result = legal_readiness(organization)
    if not organization.demo_mode and not result["ready"]:
        raise ValidationError("Producción bloqueada. Falta verificar: " + "; ".join(result["missing"]))
    return result


def require_customer_documents(subscription):
    if subscription.customer.organization.demo_mode:
        return {"ready": False, "demo_mode": True}
    require_plan_registration(subscription.plan)
    records = Consent.objects.filter(customer=subscription.customer, withdrawn_at__isnull=True, document__organization=subscription.customer.organization, document__status="approved", document__effective_on__lte=timezone.localdate())
    if not records.filter(purpose="contract", document__kind="contract").exists() or not records.filter(purpose="privacy_notice", document__kind="privacy").exists():
        raise ValidationError("Falta la aceptación del contrato registrado y la constancia de entrega del aviso de privacidad del cliente.")
    return {"ready": True, "demo_mode": False}


def require_plan_registration(plan):
    if plan.organization.demo_mode:
        return {"ready": False, "demo_mode": True}
    record = PlanRegistration.objects.filter(plan=plan, approved=True).first()
    today = timezone.localdate()
    if not record or not record.reviewed_by_id or not record.reviewed_at or not record.evidence.strip() or not record.tariff_reference.strip() or record.registered_on > today or record.effective_on > today or (record.expires_on and record.expires_on < today) or record.plan_snapshot != PlanRegistration.snapshot(plan):
        raise ValidationError("La versión exacta del plan requiere registro de tarifa revisado, vigente y coincidente con precio y velocidades.")
    return {"ready": True, "demo_mode": False}


def _monday(year, month, ordinal):
    first = date(year, month, 1)
    return first + timedelta(days=(calendar.MONDAY - first.weekday()) % 7 + 7 * (ordinal - 1))


def mexican_base_holidays(year):
    """Initial calendar only: add authority/local suspensions as BusinessHoliday entries."""
    days = {date(year, 1, 1), _monday(year, 2, 1), _monday(year, 3, 3), date(year, 5, 1), date(year, 9, 16), _monday(year, 11, 3), date(year, 12, 25)}
    if (year - 2024) % 6 == 0:
        days.add(date(year, 10, 1))
    return days


def add_business_days(start, count, organization):
    if count < 0:
        raise ValueError("count must be nonnegative")
    overrides = dict(BusinessHoliday.objects.filter(organization=organization).values_list("date", "is_working_day"))
    cursor = start
    while count:
        cursor += timedelta(days=1)
        working = overrides.get(cursor, cursor.weekday() < 5 and cursor not in mexican_base_holidays(cursor.year))
        if working:
            count -= 1
    return cursor


@transaction.atomic
def record_consent(customer, document, purpose, channel, evidence, actor=None):
    from core.services import audit
    document = DocumentVersion.objects.select_for_update().get(pk=document.pk)
    if actor and not actor.is_staff and customer.user_id != actor.pk:
        raise PermissionDenied
    if customer.organization_id != document.organization_id:
        raise ValidationError("El documento pertenece a otro operador.")
    if document.status != "approved" or document.effective_on > timezone.localdate():
        raise ValidationError("La versión no está aprobada o aún no entra en vigor.")
    expected = {"contract": "contract", "privacy_notice": "privacy"}
    if purpose in expected and document.kind != expected[purpose]:
        raise ValidationError("El tipo de documento no corresponde a la constancia.")
    record = Consent(customer=customer, document=document, purpose=purpose, channel=channel, evidence=evidence, content_snapshot=document.content, content_hash=document.content_hash, recorded_by=actor)
    record.full_clean()
    record.save()
    audit(actor, "consent.recorded", record, {"purpose": purpose, "document_version": document.version, "hash": document.content_hash})
    return record


@transaction.atomic
def withdraw_consent(consent_id, actor):
    from core.services import audit
    record = Consent.objects.select_for_update().get(pk=consent_id)
    if not actor or not actor.is_authenticated or (not actor.is_staff and record.customer.user_id != actor.pk):
        raise PermissionDenied
    if record.purpose not in {"marketing", "autopay"}:
        raise ValidationError("La constancia de entrega no se revoca; usa ARCO o cancelación del servicio según corresponda.")
    if not record.withdrawn_at:
        record.withdrawn_at = timezone.now()
        record.save(update_fields=["withdrawn_at"])
        audit(actor, "consent.withdrawn", record)
    return record


def create_arco_request(customer, request_type, description, actor=None):
    from core.services import audit
    if actor and not actor.is_staff and customer.user_id != actor.pk:
        raise PermissionDenied
    record = ARCORequest(customer=customer, request_type=request_type, description=description)
    record.response_due_on = add_business_days(record.received_on, 20, customer.organization)
    record.full_clean()
    record.save()
    audit(actor, "arco.received", record)
    return record


@transaction.atomic
def verify_arco_identity(request_id, actor, evidence):
    from core.services import audit
    record = ARCORequest.objects.select_for_update().get(pk=request_id)
    if not evidence.strip():
        raise ValidationError("Documenta la verificación de identidad o representación.")
    if record.status != "pending_identity":
        raise ValidationError("La identidad ya fue revisada.")
    record.identity_verified_at = timezone.now()
    record.identity_evidence = evidence.strip()
    record.status = "in_review"
    record.save(update_fields=["identity_verified_at", "identity_evidence", "status"])
    audit(actor, "arco.identity_verified", record)
    return record


@transaction.atomic
def respond_arco(request_id, actor, decision, granted, sent_on=None):
    from core.services import audit
    record = ARCORequest.objects.select_for_update(of=("self",)).select_related("customer__organization").get(pk=request_id)
    if record.status != "in_review" or not record.identity_verified_at:
        raise ValidationError("Verifica identidad antes de comunicar una respuesta.")
    if not decision.strip():
        raise ValidationError("Registra la respuesta y su fundamento.")
    sent_on = sent_on or timezone.localdate()
    if sent_on < record.received_on or sent_on > timezone.localdate():
        raise ValidationError("Fecha de comunicación inválida.")
    record.decision = decision.strip()
    record.granted = granted
    record.decision_sent_on = sent_on
    record.implementation_due_on = add_business_days(sent_on, 15, record.customer.organization) if granted else None
    record.status = "decision_sent" if granted else "completed"
    if not granted:
        record.completed_on = sent_on
    record.save()
    audit(actor, "arco.responded", record, {"granted": granted})
    return record


@transaction.atomic
def extend_arco(request_id, actor, stage, reason, notification_evidence, notified_on=None):
    from core.services import audit
    record = ARCORequest.objects.select_for_update(of=("self",)).select_related("customer__organization").get(pk=request_id)
    if stage not in {"response", "implementation"} or not reason.strip() or not notification_evidence.strip():
        raise ValidationError("Registra etapa, justificación y evidencia de notificación.")
    if record.extensions.filter(stage=stage).exists():
        raise ValidationError("Sólo se permite una ampliación por etapa.")
    field = "response_due_on" if stage == "response" else "implementation_due_on"
    due = getattr(record, field)
    if not due or (stage == "response" and record.status not in {"pending_identity", "in_review"}) or (stage == "implementation" and record.status != "decision_sent"):
        raise ValidationError("La etapa ya terminó o todavía no inicia.")
    notified_on = notified_on or timezone.localdate()
    if notified_on > due or notified_on > timezone.localdate() or notified_on < record.received_on:
        raise ValidationError("La ampliación debe notificarse dentro del plazo vigente.")
    extended = add_business_days(due, 20 if stage == "response" else 15, record.customer.organization)
    extension = ARCOExtension.objects.create(request=record, stage=stage, reason=reason, notification_evidence=notification_evidence, notified_on=notified_on, previous_due_on=due, extended_due_on=extended, recorded_by=actor)
    setattr(record, field, extended)
    record.save(update_fields=[field])
    audit(actor, "arco.extended", extension, {"stage": stage, "due": str(extended)})
    return extension


@transaction.atomic
def complete_arco(request_id, actor, evidence):
    from core.services import audit
    record = ARCORequest.objects.select_for_update().get(pk=request_id)
    if record.status != "decision_sent" or not evidence.strip():
        raise ValidationError("Se necesita una respuesta favorable y evidencia de ejecución.")
    record.status = "completed"
    record.completed_on = timezone.localdate()
    record.implementation_evidence = evidence.strip()
    record.save(update_fields=["status", "completed_on", "implementation_evidence"])
    audit(actor, "arco.completed", record)
    return record


def can_dispose(customer, category, retained_since=None, purpose_ended=False, as_of=None):
    """Eligibility only; never erase data. Require an explicit retention clock and ended purpose."""
    if retained_since is None or not purpose_ended:
        return False
    if hasattr(retained_since, "date"):
        retained_since = retained_since.date()
    as_of = as_of or timezone.localdate()
    if retained_since > as_of:
        return False
    if RetentionHold.objects.filter(customer=customer, released_at__isnull=True, category__in=["*", category]).exists():
        return False
    policies = list(RetentionPolicy.objects.filter(organization=customer.organization, category=category))
    return bool(policies) and all(policy.approved and policy.reviewed_on and policy.reviewed_on <= as_of and policy.lawful_basis.strip() and policy.disposal_method.strip() and as_of >= retained_since + timedelta(days=policy.retention_days) for policy in policies)


@transaction.atomic
def release_retention_hold(hold_id, actor, reason):
    from core.services import audit
    if not reason.strip():
        raise ValidationError("Registra el fundamento para liberar la conservación.")
    hold = RetentionHold.objects.select_for_update().get(pk=hold_id)
    if not hold.released_at:
        hold.released_at = timezone.now()
        hold.release_reason = reason.strip()
        hold.save(update_fields=["released_at", "release_reason"])
        audit(actor, "retention.hold_released", hold)
    return hold


@transaction.atomic
def cancel_subscription(subscription_id, actor, channel, reason=""):
    from core.models import Customer, Subscription
    from core.services import audit, publish
    customer_id = Subscription.objects.values_list("customer_id", flat=True).get(pk=subscription_id)
    Customer.objects.select_for_update().get(pk=customer_id)
    subscription = Subscription.objects.select_for_update(of=("self",)).select_related("customer").get(pk=subscription_id)
    if not actor or not actor.is_authenticated or (not actor.is_staff and subscription.customer.user_id != actor.pk):
        raise PermissionDenied
    if not channel.strip():
        raise ValidationError("Registra el canal de cancelación.")
    record, created = CancellationRequest.objects.get_or_create(subscription=subscription, defaults={"channel": channel, "reason": reason, "recorded_by": actor})
    if created:
        subscription.status = "cancelled"
        subscription.save(update_fields=["status"])
        audit(actor, "subscription.cancelled", subscription, {"folio": record.folio, "channel": channel, "network_disconnect_pending": True})
        publish(f"subscription-cancelled-{subscription.pk}", "subscription.cancelled", {"subscription_id": subscription.pk, "cancellation_id": record.pk})
    return record
