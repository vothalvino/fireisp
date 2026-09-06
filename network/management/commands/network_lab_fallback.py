"""Worker-only, operator-coordinated outage drill. Never controls containers or services."""
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.secrets import decrypt, encrypt
from core.services import audit
from network.agent_client import call_agent
from network.models import ProvisioningJob, RadiusCredential, RadiusSession, Router
from network.routeros import RouterOS
from network.execution import node_execution, require_local_router
from network.services import lab_queue_rates, mark_readiness, sync_confirmed_entitlements


class Command(BaseCommand):
    help = 'Prueba de respaldo RADIUS con interrupción web coordinada: prepare, test, cleanup. Solo laboratorio demo.'

    def add_arguments(self, parser):
        parser.add_argument('--phase', required=True, choices=['prepare', 'test', 'cleanup', 'verify-accounting'])
        parser.add_argument('--router-id', type=int, required=True)
        parser.add_argument('--job')
        parser.add_argument('--approve-lab', action='store_true')

    def emit(self, value):
        self.stdout.write(json.dumps(value, ensure_ascii=False, default=str))

    def handle(self, *args, **options):
        with node_execution():
            return self.run_phase(*args, **options)

    def run_phase(self, *args, **options):
        router = Router.objects.get(pk=options['router_id'])
        require_local_router(router)
        if not router.is_lab or not router.provisioned_at or not router.organization.demo_mode:
            raise CommandError('Se necesita un router de laboratorio demo aprovisionado.')
        if options['phase'] == 'prepare':
            if not options['approve_lab']:
                raise CommandError('La sesión temporal requiere --approve-lab; coordine por separado la interrupción web.')
            with transaction.atomic():
                if router.jobs.filter(action='lab', status='running').exists():
                    raise CommandError('Ya existe una prueba en ejecución.')
                credential = RadiusCredential.objects.create(router=router, username=f'{router.prefix}-lab-{secrets.token_hex(4)}', password_encrypted=encrypt(secrets.token_urlsafe(24)), is_lab=True, expires_at=timezone.now() + timedelta(minutes=10))
                job = ProvisioningJob.objects.create(router=router, action='lab', status='running', idempotency_key='fallback:' + secrets.token_hex(16), plan={'test_kind': 'web-outage-fallback', 'credential_id': credential.pk, 'isolated_lab_approved': True}, approved_at=timezone.now(), started_at=timezone.now(), attempts=1)
            try:
                result = sync_confirmed_entitlements()
                if not result.get('radius_ready'):
                    raise CommandError('Los listeners RADIUS privados no están listos.')
                time.sleep(4)  # Allow the independent daemon to validate/load this cache generation.
                audit(None, 'network.fallback.prepared', router, {'job': str(job.pk)})
                self.emit({'job': str(job.pk), 'prepared': True, 'expires_at': credential.expires_at, 'message': 'Interrumpa solo web durante la fase test; restaure web en un finally y ejecute cleanup.'})
            except Exception:
                self.cleanup(job, credential)
                raise CommandError('No se pudo preparar el respaldo; la credencial quedó deshabilitada.')
            return
        if not options['job']:
            raise CommandError('Indique --job de la preparación.')
        job = ProvisioningJob.objects.get(pk=options['job'], router=router, action='lab', plan__test_kind='web-outage-fallback')
        credential = RadiusCredential.objects.get(pk=job.plan['credential_id'], router=router, is_lab=True)
        if options['phase'] == 'verify-accounting':
            if not job.result.get('local_cache_auth'):
                raise CommandError('Primero complete la prueba real de autenticación con web detenida.')
            deadline = time.monotonic() + 30
            confirmed = False
            while time.monotonic() < deadline:
                confirmed = RadiusSession.objects.filter(router=router, username=credential.username, started_at__isnull=False, stopped_at__isnull=False, replayed_at__isnull=False).exists()
                if confirmed:
                    break
                time.sleep(2)
            job.result = {**job.result, 'accounting_replayed': confirmed}
            job.save(update_fields=['result'])
            mark_readiness(router, accounting_replay=confirmed)
            self.emit({'job': str(job.pk), 'accounting_replayed': confirmed})
            if not confirmed:
                raise CommandError('No se confirmó todavía Start/Stop recuperado desde el diario; revise el cursor y el servicio RADIUS.')
            return
        if options['phase'] == 'cleanup':
            self.cleanup(job, credential)
            self.emit({'job': str(job.pk), 'cleaned_up': True})
            return
        if job.status != 'running' or not credential.enabled or credential.expires_at <= timezone.now():
            raise CommandError('La preparación ya terminó o caducó; limpie y prepare otra prueba.')
        try:
            request = urllib.request.Request(os.environ.get('NETWORK_HEALTH_URL', 'http://web:8000/healthz'), headers={'Host': settings.ALLOWED_HOSTS[0], 'X-Forwarded-Proto': 'https'})
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    response.read(16384)
            except (urllib.error.URLError, TimeoutError):
                backend_unavailable = True
            else:
                raise CommandError('La aplicación todavía responde; esta fase exige una interrupción real y coordinada.')
            result = call_agent('lab_start', router.pk, username=credential.username, password=decrypt(credential.password_encrypted), service_name=router.service_name)
            with RouterOS(router) as api:
                active = any(row.get('name') == credential.username for row in api.rows('/ppp/active', ['name']))
                rates = lab_queue_rates(api, credential.username)
            result.update(backend_unavailable=backend_unavailable, router_session_observed=active, observed_rates_bps=rates, local_cache_auth=bool(result.get('ppp_up') and active and [5000000, 10000000] in rates), accounting_online=False)
            job.result = result
            job.status = 'succeeded' if result['local_cache_auth'] else 'failed'
            job.finished_at = timezone.now()
            job.save(update_fields=['result', 'status', 'finished_at'])
            mark_readiness(router, local_cache_auth=result['local_cache_auth'])
            audit(None, 'network.fallback.' + job.status, router, {'job': str(job.pk)})
            self.emit({'job': str(job.pk), 'status': job.status, 'result': result})
            if job.status != 'succeeded':
                raise CommandError('La prueba no confirmó respaldo local.')
        except Exception as exc:
            if job.status == 'running':
                job.status = 'failed'
                job.error = 'La prueba de respaldo no pudo completarse; revise servicios y el resultado seguro.'
                job.finished_at = timezone.now()
                job.save(update_fields=['status', 'error', 'finished_at'])
            if isinstance(exc, CommandError):
                raise
            raise CommandError('La prueba de respaldo falló; se limpiará la credencial y el cliente temporal.') from None
        finally:
            self.cleanup(job, credential)

    def cleanup(self, job, credential):
        try:
            call_agent('lab_stop', job.router_id)
        finally:
            credential.enabled = False
            credential.save(update_fields=['enabled'])
            sync_confirmed_entitlements()
            if job.status == 'running':
                job.status = 'failed'
                job.error = 'Prueba preparada y limpiada sin terminar.'
                job.finished_at = timezone.now()
                job.save(update_fields=['status', 'error', 'finished_at'])
