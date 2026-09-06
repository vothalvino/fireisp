from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from core.models import Subscription
from core.services import publish


@shared_task
def renewal_preview():
    """Notify impending renewals; never silently generate debt or charge a card."""
    now=timezone.now()
    count=0
    for subscription in Subscription.objects.filter(status='active',paid_until__gte=now,paid_until__lte=now+timedelta(days=7)).select_related('plan'):
        publish(f'renewal:{subscription.pk}:{subscription.paid_until.isoformat()}', 'notification',
            {'customer_id':subscription.customer_id,'title':'Tu mensualidad está por vencer',
             'body':f'Tu servicio está pagado hasta {subscription.paid_until:%d/%m/%Y}. Mensualidad: ${subscription.plan.price_mxn} MXN. Registra tu pago para renovar.'})
        count+=1
    return count


@shared_task
def evaluate_suspensions():
    """Explicit opt-in only; same immutable review and live checks as manual apply."""
    from .models import SuspensionPolicy
    from .services import suspension_block,propose_suspension,review_suspension,apply_suspension
    from django.core.exceptions import ValidationError
    now=timezone.now()
    count=0
    for policy in SuspensionPolicy.objects.filter(automatic_enabled=True).select_related('organization'):
        subscriptions=Subscription.objects.filter(customer__organization=policy.organization,status='active',paid_until__lte=now-timedelta(hours=policy.grace_hours)).select_related('customer')[:200]
        for subscription in subscriptions:
            if suspension_block(subscription,policy,now):
                continue
            try:
                proposal=propose_suspension(subscription,None,f'auto:{subscription.pk}:{int(now.timestamp()//120)}')
                review_suspension(proposal,True,'Política automática de falta de pago habilitada por administración.',None)
                result=apply_suspension(proposal,None)
                count+=result.applied
            except ValidationError:
                continue
    return count
