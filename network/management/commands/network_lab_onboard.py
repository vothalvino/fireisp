import json
import sys

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import Organization
from core.secrets import encrypt
from network.models import ProvisioningJob, Router
from network.routeros import fingerprint
from network.services import build_plan, enqueue, process_job


class Command(BaseCommand):
    help = 'Operador de laboratorio: usa los mismos modelos, planes y trabajos que la interfaz web. No imprime secretos.'

    def add_arguments(self, parser):
        parser.add_argument('--phase', required=True, choices=['register', 'trust', 'discover', 'plan', 'apply', 'verify', 'lab', 'status', 'rollback', 'retry'])
        parser.add_argument('--credentials-stdin', action='store_true')
        parser.add_argument('--router-id', type=int)
        parser.add_argument('--organization-id', type=int)
        parser.add_argument('--trust-fingerprint')
        parser.add_argument('--approve-plan')
        parser.add_argument('--approve-global', action='store_true')
        parser.add_argument('--approve-lab', action='store_true')
        parser.add_argument('--wait-interim', action='store_true', help='Espera hasta 75 segundos un Accounting-Interim real en la prueba PPPoE.')
        parser.add_argument('--source-job')

    def handle(self, *args, **opts):
        phase = opts['phase']
        if phase == 'register':
            if not opts['credentials_stdin']:
                raise CommandError('Entregue las credenciales mediante stdin, nunca argumentos de proceso.')
            try:
                data = json.loads(sys.stdin.read(16384))
                host = data.get('host') or data.get('management_host')
                username = data.get('user') or data.get('username')
                password = data['password']
                import ipaddress
                host = str(ipaddress.IPv4Address(host))
                if not username or not password or not data.get('is_lab', True):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise CommandError('JSON de credenciales inválido para laboratorio.')
            organization = Organization.objects.get(pk=opts['organization_id']) if opts['organization_id'] else Organization.objects.filter(demo_mode=True).first()
            if not organization:
                raise CommandError('Primero configure una organización de demostración.')
            router, created = Router.objects.get_or_create(management_host=host, defaults={'organization': organization, 'name': data.get('name', 'CHR laboratorio'), 'username': username, 'password_encrypted': encrypt(password), 'ssh_port': data.get('port', 22), 'is_lab': True})
            if not created and (router.username != username or router.organization_id != organization.pk):
                raise CommandError('El router existente tiene otra organización o cuenta; revise la interfaz.')
            self.run_job(enqueue(router, 'probe'))
            router.refresh_from_db()
            self.output({'router_id': router.pk, 'candidate_fingerprint': fingerprint(router.candidate_host_key), 'trusted': bool(router.trusted_host_key)})
            return
        if not opts['router_id']:
            raise CommandError('Indique --router-id.')
        router = Router.objects.get(pk=opts['router_id'])
        if phase == 'trust':
            if not opts['trust_fingerprint'] or opts['trust_fingerprint'] != fingerprint(router.candidate_host_key):
                raise CommandError('La huella revisada no coincide.')
            router.trusted_host_key = router.candidate_host_key
            router.trusted_at = timezone.now()
            router.save(update_fields=['trusted_host_key', 'trusted_at'])
            self.run_job(enqueue(router, 'discover'))
        elif phase == 'discover':
            self.run_job(enqueue(router, 'discover'))
        elif phase == 'plan':
            self.output(build_plan(router))
        elif phase == 'apply':
            plan = build_plan(router)
            if opts['approve_plan'] != plan['snapshot_hash']:
                raise CommandError('Debe revisar el plan y pasar su hash en --approve-plan.')
            plan['global_approved'] = opts['approve_global']
            self.run_job(enqueue(router, 'apply', plan=plan, key=f'{router.pk}:apply:{router.snapshot_hash}'))
        elif phase == 'verify':
            self.run_job(enqueue(router, 'verify'))
        elif phase == 'lab':
            if not opts['approve_lab']:
                raise CommandError('La sesión temporal, desconexión y reconexión requieren --approve-lab.')
            self.run_job(enqueue(router, 'lab', plan={'router_id': router.pk, 'isolated_lab_approved': True, 'wait_interim': opts['wait_interim']}))
        elif phase == 'retry':
            if not opts['source_job']:
                raise CommandError('Indique --source-job con el trabajo fallido.')
            job = ProvisioningJob.objects.get(pk=opts['source_job'], router=router, status='failed')
            job.status = 'pending'
            job.save(update_fields=['status'])
            self.run_job(job)
        elif phase == 'rollback':
            if not opts['source_job']:
                raise CommandError('Indique --source-job con el trabajo aprobado a revertir.')
            self.run_job(enqueue(router, 'rollback', plan={'source_job': opts['source_job']}, key=f'rollback:{opts["source_job"]}'))
        elif phase == 'status':
            self.output({'router_id': router.pk, 'trusted_fingerprint': fingerprint(router.trusted_host_key), 'snapshot_hash': router.snapshot_hash, 'readiness': router.readiness, 'jobs': list(router.jobs.values('id', 'action', 'status', 'result', 'error')[:12])})

    def run_job(self, job):
        process_job(job)
        self.output({'job': str(job.pk), 'action': job.action, 'status': job.status, 'result': job.result, 'error': job.error})
        if job.status == 'failed':
            raise CommandError('El trabajo falló; vea el diagnóstico seguro anterior.')

    def output(self, value):
        self.stdout.write(json.dumps(value, ensure_ascii=False, default=str))
