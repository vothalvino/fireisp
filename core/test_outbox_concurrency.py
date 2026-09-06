from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.db import connections, close_old_connections, transaction
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from .models import Notification, OutboxEvent
from .tasks import deliver_outbox


def notification_event(key):
    return OutboxEvent.objects.create(key=key, topic='notification',
        payload={'customer_id': None, 'title': 'Aviso', 'body': 'Mensaje de prueba'})


def deliver_with_own_connection(barrier=None):
    close_old_connections()
    try:
        if barrier:
            barrier.wait(timeout=10)
        return deliver_outbox()
    finally:
        connections.close_all()


class OutboxCandidateTests(TestCase):
    def test_candidate_snapshot_cannot_bypass_new_backoff_or_attempt_limit(self):
        original = QuerySet.values_list
        for index, updates in enumerate(({'available_at': timezone.now()+timedelta(minutes=5)}, {'attempts': 5})):
            with self.subTest(updates=updates):
                event = notification_event(f'candidate-{index}')
                def changed_after_selection(queryset, *args, **kwargs):
                    result = original(queryset, *args, **kwargs)
                    if queryset.model is OutboxEvent:
                        result = list(result)
                        OutboxEvent.objects.filter(pk=event.pk).update(**updates)
                    return result
                with patch.object(QuerySet, 'values_list', new=changed_after_selection):
                    self.assertEqual(deliver_outbox(), 0)
                event.refresh_from_db()
                self.assertIsNone(event.delivered_at)
                self.assertEqual(event.attempts, updates.get('attempts', 0))
                self.assertFalse(Notification.objects.filter(source=event).exists())


@skipUnlessDBFeature('has_select_for_update_skip_locked')
class OutboxConcurrencyTests(TransactionTestCase):
    def test_locked_event_does_not_block_other_worker_and_is_delivered_later(self):
        first, second = notification_event('held'), notification_event('free')
        pool = ThreadPoolExecutor(max_workers=1)
        try:
            with transaction.atomic():
                OutboxEvent.objects.select_for_update().get(pk=first.pk)
                future = pool.submit(deliver_with_own_connection)
                self.assertEqual(future.result(timeout=10), 1)
                second.refresh_from_db()
                self.assertIsNotNone(second.delivered_at)
                self.assertFalse(Notification.objects.filter(source=first).exists())
        finally:
            # The outer transaction has released its lock even if the assertion timed out.
            pool.shutdown(wait=True)
        self.assertEqual(deliver_outbox(), 1)
        self.assertEqual(deliver_outbox(), 0)
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(list(OutboxEvent.objects.order_by('pk').values_list('attempts', flat=True)), [1, 1])

    def test_two_workers_deliver_each_event_exactly_once(self):
        for index in range(20):
            notification_event(f'parallel-{index}')
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(deliver_with_own_connection, barrier) for _ in range(2)]
            delivered = sum(future.result(timeout=15) for future in futures)
        self.assertEqual(delivered, 20)
        self.assertEqual(Notification.objects.count(), 20)
        self.assertEqual(OutboxEvent.objects.filter(delivered_at__isnull=False, attempts=1).count(), 20)
        self.assertEqual(deliver_outbox(), 0)
