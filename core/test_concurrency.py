from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest import skipUnless
from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from billing.models import Invoice, Payment
from billing.services import receive_payment
from core.models import Customer, Organization, Plan, Subscription
from core.services import activate_subscription

@skipUnless(connection.vendor == 'postgresql', 'PostgreSQL row locking is required')
class BillingConcurrencyTests(TransactionTestCase):
    def setUp(self):
        org = Organization.objects.create(name='Concurrency test', demo_mode=True)
        self.customer = Customer.objects.create(organization=org, code='CONCURRENT', name='Synthetic', address='Synthetic')
        plan = Plan.objects.create(organization=org, name='Concurrent', download_mbps=30, upload_mbps=10, price_mxn=Decimal('399'))
        self.sub = Subscription.objects.create(customer=self.customer, plan=plan, access_username='concurrent')

    def concurrently(self, first, second):
        barrier = Barrier(2)
        def execute(function):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return function()
            finally: close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(execute, function) for function in (first, second)]
            return [task.result(timeout=30) for task in tasks]

    def test_duplicate_payment_posts_once(self):
        def pay():
            return receive_payment(Customer.objects.get(pk=self.customer.pk), '399', 'transfer', None, 'same-request').pk
        ids = self.concurrently(pay, pay)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(Payment.objects.count(), 1)

    def test_payment_and_activation_do_not_deadlock_or_double_bill(self):
        self.concurrently(lambda: receive_payment(Customer.objects.get(pk=self.customer.pk), '399', 'transfer', None, 'activation-race').pk,
                          lambda: activate_subscription(self.sub.pk).pk)
        invoice = Invoice.objects.get(subscription=self.sub)
        self.assertEqual(invoice.balance, Decimal(0))
        self.sub.refresh_from_db()
        self.assertGreater(self.sub.paid_until, self.sub.activated_at)
