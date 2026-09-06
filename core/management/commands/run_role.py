import sys
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from core.runtime import ROLE_QUEUES, supervise


class Command(BaseCommand):
    help = 'Ejecuta una función de trabajo local o en otra instancia de la misma instalación.'

    def add_arguments(self, parser):
        parser.add_argument('--role', required=True, choices=ROLE_QUEUES)
        parser.add_argument('--concurrency', type=int, default=1)

    def handle(self, *args, **options):
        if not 1 <= options['concurrency'] <= 32:
            raise CommandError('La concurrencia debe ser de 1 a 32.')
        role = options['role']
        command = [sys.executable, '-m', 'celery', '-A', 'fireisp.celery', 'worker',
                   '--queues', ROLE_QUEUES[role], '--concurrency', str(options['concurrency']),
                   '--hostname', f'{settings.FIREISP_NODE_ID}-{role}@%h', '--loglevel', 'warning']
        try:
            supervise(command, role)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from None
