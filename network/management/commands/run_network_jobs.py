import time
import os
import json
import urllib.request
from django.conf import settings
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from core.models import HealthCheck
from network.models import ProvisioningJob
from network.services import process_job, sync_confirmed_entitlements


class Command(BaseCommand):
    help = 'Procesa trabajos durables de red. Ejecute una sola instancia del worker.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--recover-stale', action='store_true', help='Devuelve trabajos interrumpidos hace más de 15 minutos a pendientes.')

    def handle(self, *args, **options):
        if options['recover_stale']:
            ProvisioningJob.objects.filter(status='running', started_at__lt=timezone.now() - timedelta(minutes=15)).update(status='pending')
        last_sync = 0
        while True:
            if time.monotonic() - last_sync >= 10:
                try:
                    confirmed = sync_confirmed_entitlements()
                    health_request = urllib.request.Request(os.environ.get('NETWORK_HEALTH_URL', 'http://web:8000/healthz'), headers={'Host': settings.ALLOWED_HOSTS[0], 'X-Forwarded-Proto': 'https'})
                    with urllib.request.urlopen(health_request, timeout=3) as response:
                        control = json.loads(response.read(16384))
                    confirmed['control_plane_ready'] = bool(control.get('application_ready') and control.get('database_ready'))
                    ready = confirmed['control_plane_ready'] and confirmed.get('radius_ready', False)
                    HealthCheck.objects.update_or_create(code='network_sync', defaults={'status': 'ok' if ready else 'error', 'details': confirmed})
                except Exception:
                    try:
                        HealthCheck.objects.update_or_create(code='network_sync', defaults={'status': 'error', 'details': {'message': 'La sincronización de autorizaciones confirmadas falló; conserve el estado previo y congele suspensiones automáticas.'}})
                    except Exception:
                        pass
                    self.stderr.write('La instantánea confirmada RADIUS no se actualizó; se conserva la anterior.')
                last_sync = time.monotonic()
            with transaction.atomic():
                job = ProvisioningJob.objects.select_for_update(of=('self',)).select_related('router', 'actor').filter(status='pending').order_by('created_at').first()
                if job:
                    job.status = 'running'
                    job.save(update_fields=['status'])
            if job:
                process_job(job, claimed=True)
                self.stdout.write(f'{job.pk} {job.action} {job.status}')
            if options['once']:
                return
            if not job:
                time.sleep(2)
