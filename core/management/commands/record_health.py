import json
import sys
from django.core.management.base import BaseCommand, CommandError
from core.models import HealthCheck

class Command(BaseCommand):
    help = 'Record a predefined installer diagnostic from stdin; never takes credentials.'
    def handle(self, *args, **options):
        value = json.loads(sys.stdin.read(16000))
        if value.get('code') not in {'backup', 'restore', 'offsite', 'scale', 'security', 'browser'}:
            raise CommandError('Unknown diagnostic')
        if value.get('status') not in {'ok', 'failed', 'pending'}: raise CommandError('Invalid status')
        HealthCheck.objects.update_or_create(code=value['code'], defaults={'status': value['status'], 'details': value.get('details', {})})
        self.stdout.write('Diagnostic recorded.')
