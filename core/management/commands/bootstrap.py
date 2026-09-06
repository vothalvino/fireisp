import os
from pathlib import Path
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from core.models import Branch, Organization
from core.services import invite

class Command(BaseCommand):
    help = 'Initialize one ISP and write a one-use administrator invitation to a private file.'
    def add_arguments(self, parser):
        parser.add_argument('--username', default='administrador')
        parser.add_argument('--url', default='https://demo2.opentrk.com.mx')
        parser.add_argument('--invitation-file', required=True)
        parser.add_argument('--renew-invitation', action='store_true')
    @transaction.atomic
    def handle(self, *args, **options):
        org, _ = Organization.objects.get_or_create(pk=1, defaults={'name': 'FireISP', 'demo_mode': True})
        Branch.objects.get_or_create(organization=org, name='Cuauhtémoc', defaults={'address': 'Cuauhtémoc, Chihuahua, México'})
        for role in ['Administración', 'Cobranza', 'Red', 'Soporte', 'Cumplimiento']: Group.objects.get_or_create(name=role)
        user, created = get_user_model().objects.get_or_create(username=options['username'], defaults={'is_superuser': True, 'is_staff': True, 'is_active': False})
        if not created and not options['renew_invitation']:
            self.stdout.write('Installation already initialized. No account was modified.')
            return
        if not user.is_superuser: raise CommandError('Existing username is not an administrator.')
        token = invite(user, hours=24)
        path = Path(options['invitation_file'])
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream: stream.write(options['url'].rstrip('/') + '/activate/' + token + '/\n')
        self.stdout.write('Administrator invitation written to protected file (expires in 24 hours).')
