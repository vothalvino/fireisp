import json
import time
from datetime import timedelta
from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone
from billing.models import Invoice, Payment
from billing.services import anniversary, create_period, receive_payment
from core.models import Customer, Organization, Plan, Subscription

class Command(BaseCommand):
    help = 'Measure the complete billing path in an EMPTY, isolated fireisp_bench_* database.'
    def add_arguments(self, parser):
        parser.add_argument('--customers', type=int, default=20000)
    def handle(self, *args, **options):
        if connection.vendor != 'postgresql' or not connection.settings_dict['NAME'].startswith('fireisp_bench_'):
            raise CommandError('Only a dedicated PostgreSQL fireisp_bench_* database is allowed.')
        if Organization.objects.exists(): raise CommandError('Benchmark requires an empty application database.')
        count = options['customers']
        if not 1 <= count <= 100000: raise CommandError('Invalid fixture count.')
        org = Organization.objects.create(name='Isolated benchmark', demo_mode=True)
        plan = Plan.objects.create(organization=org, name='Benchmark 50', download_mbps=50, upload_mbps=20, price_mxn=Decimal('549'))
        customers = Customer.objects.bulk_create([Customer(organization=org, code=f'BENCH-{i:06}', name=f'Synthetic benchmark {i}', address='No real address') for i in range(count)], batch_size=1000)
        activated = timezone.now() - timedelta(days=2)
        subscriptions = Subscription.objects.bulk_create([Subscription(customer=customer, plan=plan, status='active', activated_at=activated, access_username=f'bench-{customer.pk}') for customer in customers], batch_size=1000)
        start = timezone.localtime(activated).date()
        end = anniversary(start, 1)
        before = time.monotonic()
        for sub in subscriptions:
            create_period(sub, start, end)
        charges_seconds = time.monotonic() - before
        before = time.monotonic()
        for customer in customers:
            receive_payment(customer, Decimal('549'), 'transfer', None, f'bench-pay-{customer.pk}', 'SYNTHETIC')
        payments_seconds = time.monotonic() - before
        unpaid = Invoice.objects.exclude(status='paid').count()
        without_entitlement = Subscription.objects.filter(paid_until__isnull=True).count()
        total = Invoice.objects.count()
        if unpaid or without_entitlement or total != count or Payment.objects.count() != count:
            raise CommandError('Billing invariants failed.')
        summary = {'customers': count, 'charges_seconds': round(charges_seconds, 2), 'payments_seconds': round(payments_seconds, 2),
                   'total_seconds': round(charges_seconds + payments_seconds, 2), 'unpaid': unpaid, 'missing_paid_until': without_entitlement,
                   'target_30_minutes_met': charges_seconds + payments_seconds <= 1800, 'pac_calls': 0}
        self.stdout.write(json.dumps(summary))
