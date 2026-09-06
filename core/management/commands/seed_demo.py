from decimal import Decimal
from django.core.management.base import BaseCommand, CommandError
from core.models import Branch, Customer, Organization, Plan, Subscription

class Command(BaseCommand):
    help = 'Create clearly identified demonstration records; safe to repeat.'
    def handle(self, *args, **options):
        org = Organization.objects.get(pk=1)
        if not org.demo_mode: raise CommandError('Demo data is disabled outside demo mode.')
        branch = Branch.objects.filter(organization=org).first()
        plans = []
        for name, down, up, price in [('Hogar 30', 30, 10, '399'), ('Hogar 50', 50, 20, '549'), ('Negocio 100', 100, 40, '899')]:
            plans.append(Plan.objects.get_or_create(organization=org, name=name, defaults={'download_mbps': down, 'upload_mbps': up, 'price_mxn': Decimal(price)})[0])
        for i, name in enumerate(['Cliente de demostración A', 'Cliente de demostración B', 'Negocio de demostración'], 1):
            customer, _ = Customer.objects.get_or_create(code=f'DEMO-{i:03}', defaults={'organization': org, 'branch': branch, 'name': name, 'address': 'Domicilio ficticio · Cuauhtémoc, Chihuahua', 'email': f'demo{i}@example.invalid'})
            Subscription.objects.get_or_create(access_username=f'demo-{i:03}@fireisp', defaults={'customer': customer, 'plan': plans[i-1]})
        self.stdout.write('Demo customers and plans ready. No payments, invoices or network activations were fabricated.')
