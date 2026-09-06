import sys
import time
from django.core.management.base import BaseCommand, CommandError
from core.runtime import heartbeat, scheduler_lock, supervise


class Command(BaseCommand):
    help = 'Programa tareas con un único líder PostgreSQL y espera como reserva si ya existe otro.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Comprueba la propiedad y sale si otro líder está activo.')

    def handle(self, *args, **options):
        try:
            while True:
                with scheduler_lock() as ownership:
                    if ownership:
                        supervise([sys.executable, '-m', 'celery', '-A', 'fireisp.celery', 'beat',
                                   '--schedule', '/tmp/celerybeat-schedule', '--loglevel', 'warning'],
                                  'scheduler', ownership_check=ownership)
                        return
                    heartbeat('scheduler', status='standby')
                if options['once']:
                    return
                time.sleep(5)
        except RuntimeError as exc:
            raise CommandError(str(exc)) from None
