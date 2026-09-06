import re
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import DeploymentState


class Command(BaseCommand):
    help = 'Registra la versión común después de migrar y drenar los procesos de la versión anterior.'

    def add_arguments(self, parser):
        parser.add_argument('--release', required=True)

    def handle(self, *args, **options):
        release = options['release']
        if not re.fullmatch(r'[0-9a-f]{40}', release):
            raise CommandError('Especifica el SHA completo de la versión revisada.')
        with transaction.atomic():
            DeploymentState.objects.update_or_create(pk=1, defaults={'release': release})
        self.stdout.write('Versión de la instalación registrada.')
