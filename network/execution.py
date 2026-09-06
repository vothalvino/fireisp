"""One active executor per network node; distinct nodes execute independently.

PostgreSQL session locks prevent overlapping normal operations. A durable lease
and generation fence stop a former owner after a lost DB connection. Remote NAS
commands cannot be rolled back by PostgreSQL: interrupted jobs require review.
"""
import hashlib
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from .models import NetworkNode, ProvisioningJob, Router
from .routeros import RouterError

_current = ContextVar('network_executor', default=None)
LEASE_SECONDS = 300


class NodeBusy(RouterError):
    pass


class LeaseLost(RouterError):
    pass


def configured_node_id():
    return getattr(settings, 'NETWORK_NODE_ID', 'primary')


def require_local_router(router):
    if router.network_node_id != configured_node_id():
        raise RouterError('Este router pertenece a otro servidor de red; ejecute el trabajo en su nodo asignado.')


class NodeLease:
    def __init__(self, node, token, generation, db_connection):
        self.node = node
        self.token = token
        self.generation = generation
        self.db_connection = db_connection

    def check(self):
        # Reconnecting silently would release the advisory lock. Never continue
        # network effects on a new DB connection, even if the row still matches.
        if connection.connection is not self.db_connection:
            raise LeaseLost('El ejecutor perdió su conexión exclusiva; revise el trabajo interrumpido.')
        now = timezone.now()
        updated = NetworkNode.objects.filter(pk=self.node.pk, worker_token=self.token, generation=self.generation, lease_expires_at__gt=now).update(lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
        if not updated:
            raise LeaseLost('La reserva de ejecución caducó o pertenece a otro ejecutor; revise el trabajo interrumpido.')


def check_current_lease():
    lease = _current.get()
    if lease:
        lease.check()


def current_lease():
    return _current.get()


@contextmanager
def node_execution(node_id=None):
    node_id = node_id or configured_node_id()
    if node_id != configured_node_id():
        raise RouterError('El ejecutor no puede operar otro servidor de red.')
    existing = _current.get()
    if existing:
        if existing.node.pk != node_id:
            raise RouterError('No se pueden mezclar nodos en una ejecución.')
        existing.check()
        yield existing
        return
    # Only the seeded primary node can be created implicitly (test databases
    # may flush data migrations). Additional nodes require explicit registration.
    if node_id == 'primary':
        NetworkNode.objects.get_or_create(pk='primary', defaults={'name': 'Servidor principal'})
    lock_key = int.from_bytes(hashlib.sha256(('fireisp-network:' + node_id).encode()).digest()[:8], 'big', signed=True)
    pg_lock = connection.vendor == 'postgresql'
    db_connection = None
    acquired = False
    lease = None
    context_token = None
    try:
        if pg_lock:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s)', [lock_key])
                acquired = cursor.fetchone()[0]
            if not acquired:
                raise NodeBusy('Otro ejecutor ya atiende este nodo de red.')
        with transaction.atomic():
            node = NetworkNode.objects.select_for_update().get(pk=node_id)
            now = timezone.now()
            if node.worker_token and node.lease_expires_at and node.lease_expires_at > now:
                raise NodeBusy('El nodo conserva una reserva activa; espere su liberación o caducidad.')
            node.worker_token = uuid.uuid4()
            node.generation += 1
            node.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            node.save(update_fields=['worker_token', 'generation', 'lease_expires_at'])
        db_connection = connection.connection
        lease = NodeLease(node, node.worker_token, node.generation, db_connection)
        context_token = _current.set(lease)
        yield lease
    finally:
        if context_token is not None:
            _current.reset(context_token)
        if lease:
            try:
                NetworkNode.objects.filter(pk=node_id, worker_token=lease.token, generation=lease.generation).update(worker_token=None, lease_expires_at=None)
            except Exception:
                # A crash/lost DB connection leaves the durable lease to expire.
                pass
        if pg_lock and acquired and connection.connection is db_connection and db_connection is not None:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [lock_key])
        elif pg_lock and acquired and lease is None:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [lock_key])


def recover_interrupted_jobs(lease):
    """Quarantine uncertain effects; never blindly replay a vanished worker."""
    lease.check()
    with transaction.atomic():
        jobs = ProvisioningJob.objects.filter(router__network_node_id=lease.node.pk, status='running').exclude(worker_token=lease.token).exclude(plan__has_key='test_kind', plan__test_kind='web-outage-fallback', started_at__gt=timezone.now() - timedelta(minutes=10))
        router_ids = list(jobs.values_list('router_id', flat=True))
        count = jobs.update(status='failed', error='La ejecución anterior se interrumpió. Revise el NAS y el agente antes de reintentar; otros trabajos de este router están detenidos.', finished_at=timezone.now())
        Router.objects.filter(pk__in=router_ids).update(execution_blocked=True)
    return count
