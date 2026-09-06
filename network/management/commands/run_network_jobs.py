import json
import os
import signal
import time
import threading
import urllib.request

from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import HealthCheck
from core.runtime import heartbeat
from network.execution import NodeBusy, configured_node_id, node_execution, recover_interrupted_jobs
from network.models import ProvisioningJob
from network.services import process_job, sync_confirmed_entitlements


class Command(BaseCommand):
    help = 'Atiende el nodo NETWORK_NODE_ID. PostgreSQL serializa ejecutores del mismo nodo; nodos distintos trabajan en paralelo.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--recover-stale', action='store_true', help='Compatibilidad: los trabajos interrumpidos se detienen para revisión, nunca se repiten automáticamente.')

    def handle(self, *args, **options):
        self.stopping = False
        previous = {}
        if threading.current_thread() is threading.main_thread():
            def stop(signum, frame):
                self.stopping = True
            previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            self.run_loop(options)
        finally:
            try:
                heartbeat('network', status='stopped')
            except Exception:
                pass
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def run_loop(self, options):
        last_sync = 0
        while not self.stopping:
            job = None
            try:
                with node_execution() as lease:
                    heartbeat('network')
                    recover_interrupted_jobs(lease)
                    if time.monotonic() - last_sync >= 10:
                        try:
                            confirmed = sync_confirmed_entitlements()
                            health_request = urllib.request.Request(os.environ.get('NETWORK_HEALTH_URL', 'http://web:8000/healthz'), headers={'Host': settings.ALLOWED_HOSTS[0], 'X-Forwarded-Proto': 'https'})
                            with urllib.request.urlopen(health_request, timeout=3) as response:
                                control = json.loads(response.read(16384))
                            confirmed['control_plane_ready'] = bool(control.get('application_ready') and control.get('database_ready'))
                            ready = confirmed['control_plane_ready'] and confirmed.get('radius_ready', False)
                            lease.check()
                            HealthCheck.objects.update_or_create(code=lease.node.health_code, defaults={'status': 'ok' if ready else 'error', 'details': confirmed})
                        except Exception:
                            lease.check()
                            HealthCheck.objects.update_or_create(code=lease.node.health_code, defaults={'status': 'error', 'details': {'network_node_id': lease.node.pk, 'message': 'La sincronización de autorizaciones confirmadas falló; conserve el estado previo y congele suspensiones automáticas.'}})
                            self.stderr.write('La instantánea confirmada RADIUS no se actualizó; se conserva la anterior.')
                        last_sync = time.monotonic()
                    job = ProvisioningJob.objects.select_related('router', 'actor').filter(status='pending', router__execution_blocked=False, router__network_node_id=configured_node_id()).exclude(router__jobs__status='running').order_by('created_at').first()
                    if job:
                        process_job(job)
                        self.stdout.write(f'{job.pk} {job.action} {job.status}')
            except NodeBusy:
                pass  # Another executor owns this node. Distinct nodes are independent.
            if options['once']:
                return
            if not job:
                time.sleep(2)
