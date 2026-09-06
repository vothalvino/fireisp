from celery import shared_task
from datetime import timedelta
from django.db import transaction
from django.utils import timezone
from .models import Notification, OutboxEvent

@shared_task
def deliver_outbox():
    count = 0
    for event_id in OutboxEvent.objects.filter(delivered_at__isnull=True, available_at__lte=timezone.now(), attempts__lt=5).values_list('pk', flat=True)[:100]:
        with transaction.atomic():
            event = OutboxEvent.objects.select_for_update(skip_locked=True).filter(pk=event_id).first()
            if event is None: continue
            if event.delivered_at or event.available_at > timezone.now() or event.attempts >= 5: continue
            event.attempts += 1
            try:
                with transaction.atomic():
                    if event.topic == 'notification':
                        Notification.objects.get_or_create(source=event, defaults={k: event.payload[k] for k in ('customer_id', 'title', 'body')})
                    elif event.topic in ('subscription.cancelled', 'subscription.changed'):
                        from network.services import queue_subscription_sync
                        result = queue_subscription_sync(event.payload['subscription_id'])
                        if event.topic == 'subscription.cancelled' and not result['disconnect_pending']:
                            from compliance.models import CancellationRequest
                            CancellationRequest.objects.filter(pk=event.payload['cancellation_id']).update(network_disconnect_pending=False)
                    else:
                        raise ValueError('Unrecognized topic')
                    event.delivered_at = timezone.now()
                    event.error = ''
                    count += 1
            except Exception as exc:
                event.error = type(exc).__name__ + ': requiere revisión del evento.'
                event.available_at = timezone.now() + timedelta(seconds=min(3600, 30 * 2 ** event.attempts))
            event.save()
    return count
