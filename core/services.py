import hashlib
import secrets
from datetime import timedelta
from django.db import transaction
from django.core.exceptions import ValidationError
from django.utils import timezone
from .models import ActivationToken, AuditEvent, OutboxEvent, Subscription

def audit(actor, action, target, details=None):
    def redact(value):
        if isinstance(value, dict):
            return {str(k): '[protegido]' if any(s in str(k).lower() for s in ('password', 'token', 'secret', 'private_key', 'credential')) else redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)): return [redact(v) for v in value]
        if value is None or isinstance(value, (str, bool, int, float)): return value
        return str(value)
    return AuditEvent.objects.create(actor=actor if getattr(actor, 'is_authenticated', False) else None,
        action=action, target=str(target), details=redact(details or {}))

def invite(user, actor=None, hours=24):
    with transaction.atomic():
        ActivationToken.objects.filter(user=user, used_at__isnull=True).update(used_at=timezone.now())
        value = secrets.token_urlsafe(32)
        ActivationToken.objects.create(user=user, digest=hashlib.sha256(value.encode()).hexdigest(), expires_at=timezone.now() + timedelta(hours=hours))
        audit(actor, 'account.invited', user.pk)
        return value

def publish(key, topic, payload):
    return OutboxEvent.objects.get_or_create(key=key, defaults={'topic': topic, 'payload': payload})[0]

@transaction.atomic
def activate_subscription(subscription_id, actor=None):
    from .models import Customer
    customer_id = Subscription.objects.values_list('customer_id', flat=True).get(pk=subscription_id)
    Customer.objects.select_for_update().get(pk=customer_id)
    sub = Subscription.objects.select_for_update(of=("self",)).select_related('customer__organization', 'plan').get(pk=subscription_id)
    if sub.status == 'cancelled': raise ValidationError('Un servicio cancelado no puede activarse.')
    if sub.activated_at: return sub
    from compliance.services import require_customer_documents, require_legal_readiness
    require_legal_readiness(sub.customer.organization)
    require_customer_documents(sub)
    from operations.services import require_installation_readiness
    require_installation_readiness(sub)
    sub.activated_at = timezone.now()
    sub.status = 'active'
    sub.save(update_fields=['activated_at', 'status'])
    from billing.services import anniversary, create_period
    start = timezone.localtime(sub.activated_at).date()
    create_period(sub, start, anniversary(start, 1), actor)
    audit(actor, 'subscription.activated', sub.pk, {'activated_at': sub.activated_at})
    publish(f'activation:{sub.pk}', 'notification', {'customer_id': sub.customer_id, 'title': 'Tu servicio está activo', 'body': 'Registramos la activación de tu conexión. Consulta la vigencia en tu portal.'})
    publish(f'access-activation:{sub.pk}', 'subscription.changed', {'subscription_id': sub.pk})
    sub.refresh_from_db()
    return sub
