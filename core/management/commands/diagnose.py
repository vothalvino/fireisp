import json
from django.core.management.base import BaseCommand
from django.db import connection
from django.core.cache import cache
from django.utils import timezone
from core.models import HealthCheck, Organization, OutboxEvent
from compliance.services import legal_readiness
from fiscal.models import FiscalProfile
from network.models import Router, ProvisioningJob

class Command(BaseCommand):
    help = 'Report safe application readiness without emitting credentials or customer data.'
    def handle(self, *args, **options):
        with connection.cursor() as cursor: cursor.execute('SELECT 1')
        cache.set('diagnose', True, 10)
        org = Organization.objects.first()
        profile = FiscalProfile.objects.first()
        result = {'database': True, 'cache': bool(cache.get('diagnose')), 'legal': legal_readiness(org) if org else {'ready': False},
                  'finkok_credentials_verified': bool(profile and profile.verified_at),
                  'routers': list(Router.objects.values('id', 'name', 'is_lab', 'readiness')),
                  'failed_network_jobs': ProvisioningJob.objects.filter(status='failed').count(),
                  'exhausted_events': OutboxEvent.objects.filter(delivered_at__isnull=True, attempts__gte=5).count(),
                  'host_checks': list(HealthCheck.objects.values('code', 'status', 'checked_at', 'details')),
                  'checked_at': timezone.now().isoformat()}
        self.stdout.write(json.dumps(result, default=str, ensure_ascii=False))
