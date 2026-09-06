"""Process roles share records and queues while retaining independent placement."""
import os
import re
import signal
import socket
import subprocess
import time
from contextlib import contextmanager

from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import DeploymentState, RuntimeNode

ROLE_QUEUES = {'worker': 'core', 'billing': 'billing', 'fiscal': 'fiscal'}


def validate_release():
    expected = DeploymentState.objects.filter(pk=1).values_list('release', flat=True).first()
    actual = settings.FIREISP_RELEASE
    if expected and expected != actual:
        raise RuntimeError('Esta instancia usa una versión distinta a la instalación principal.')
    if not expected and not (settings.DEBUG or settings.TESTING):
        raise RuntimeError('Inicializa la versión de la instalación antes de arrancar sus procesos.')
    return actual


def heartbeat(role, node_id=None, status='ready'):
    release = validate_release()
    node_id = node_id or settings.FIREISP_NODE_ID
    if role not in {*ROLE_QUEUES, 'scheduler', 'network', 'web'} or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', node_id):
        raise ValueError('Identidad o función de instancia inválida.')
    node, _ = RuntimeNode.objects.update_or_create(identifier=f'{node_id}:{role}', defaults={
        'role': role, 'release': release, 'hostname': socket.gethostname()[:120],
        'status': status, 'last_seen': timezone.now(),
    })
    return node


@contextmanager
def scheduler_lock():
    """Use one dedicated DB session: a lost connection cannot silently reacquire it."""
    if connection.vendor != 'postgresql':
        raise RuntimeError('El programador requiere PostgreSQL para elegir un único proceso activo.')
    import psycopg
    params = connection.get_connection_params()
    params.update(connect_timeout=5, keepalives_idle=5, keepalives_interval=2,
                  keepalives_count=2, tcp_user_timeout=10000)
    raw = psycopg.connect(**params, autocommit=True)
    try:
        with raw.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(760130, 1)')
            owned = cursor.fetchone()[0]
        def check():
            with raw.cursor() as cursor:
                cursor.execute('SELECT 1')
                return cursor.fetchone()[0] == 1
        yield check if owned else None
    finally:
        raw.close()


def supervise(command, role, *, ownership_check=None, poll_seconds=5):
    """Drain workers on shutdown; stop scheduling immediately if leadership is lost."""
    heartbeat(role)
    stopping = False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    child = None
    last_heartbeat = 0
    failed = False
    try:
        if ownership_check and not ownership_check():
            failed = True
            raise RuntimeError('Se perdió la propiedad del programador antes de arrancarlo.')
        child = subprocess.Popen(command, start_new_session=True)
        while child.poll() is None and not stopping:
            try:
                if ownership_check and not ownership_check():
                    raise RuntimeError('Se perdió la propiedad del programador.')
                if time.monotonic() - last_heartbeat >= 20:
                    heartbeat(role)
                    last_heartbeat = time.monotonic()
            except Exception:
                failed = True
                break
            time.sleep(poll_seconds)
        if child.poll() is not None and child.returncode:
            failed = True
    except Exception:
        failed = True
        raise
    finally:
        if child and child.poll() is None:
            # Celery's parent must warm-drain its prefork pool itself.
            try:
                child.terminate()
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=5 if role == 'scheduler' else 140)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=10)
        try:
            heartbeat(role, status='failed' if failed else 'stopped')
        except Exception:
            pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if failed:
        raise RuntimeError('El proceso se detuvo; verifica su conexión y versión antes de reintentarlo.')
