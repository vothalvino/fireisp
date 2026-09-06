import hashlib
import os
import sys

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.services import audit
from network.models import NetworkNode


class Command(BaseCommand):
    help = 'Registra un servidor de red y su token exclusivo. No mueve routers ni imprime secretos.'

    def add_arguments(self, parser):
        parser.add_argument('node_id')
        parser.add_argument('--endpoint', required=True)
        parser.add_argument('--name', default='')
        parser.add_argument('--radius-token-stdin', action='store_true')

    def handle(self, *args, **options):
        if not options['radius_token_stdin']:
            raise CommandError('Entregue el token mediante --radius-token-stdin, nunca argumentos de proceso.')
        token = sys.stdin.read(1025).strip()
        if not 32 <= len(token) <= 512 or any(c.isspace() for c in token):
            raise CommandError('El token debe tener entre 32 y 512 caracteres sin espacios.')
        with transaction.atomic():
            node, _ = NetworkNode.objects.select_for_update().get_or_create(pk=options['node_id'])
            if node.worker_token and node.lease_expires_at and node.lease_expires_at > timezone.now():
                raise CommandError('Detenga el ejecutor de este nodo antes de actualizar su registro.')
            legacy_primary = node.pk == 'primary' and not node.public_endpoint and options['endpoint'] == os.environ.get('NETWORK_PUBLIC_ENDPOINT')
            if node.public_endpoint != options['endpoint'] and not legacy_primary and node.routers.filter(provisioned_at__isnull=False).exists():
                raise CommandError('El nodo tiene routers aprovisionados. Su endpoint exige un plan de migración de red revisado.')
            digest = hashlib.sha256(token.encode()).hexdigest()
            if NetworkNode.objects.exclude(pk=node.pk).filter(radius_token_digest=digest).exists():
                raise CommandError('Cada nodo necesita un token diferente.')
            node.public_endpoint = options['endpoint']
            node.name = options['name'] or node.name or node.pk
            node.radius_token_digest = digest
            try:
                node.full_clean()
            except ValidationError as exc:
                raise CommandError('Identificador, nombre o dirección del nodo inválidos.') from exc
            node.save(update_fields=['name', 'public_endpoint', 'radius_token_digest'])
            audit(None, 'network.node.registered', node.pk, {'endpoint': node.public_endpoint})
        self.stdout.write(f'Nodo de red {node.pk} registrado. Ningún router fue movido.')
