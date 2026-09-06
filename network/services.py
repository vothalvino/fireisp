import copy
import hashlib
import ipaddress
import json
import os
import re
import secrets
import time
from datetime import timedelta

from django.utils import timezone
from django.db import transaction

from core.secrets import decrypt, encrypt
from core.services import audit
from .agent_client import MAX_ENTITLEMENT_ENTRIES, AgentError, call_agent
from .models import NetworkNode, ProvisioningJob, RadiusCredential, RadiusSession, Router
from .execution import NodeBusy, check_current_lease, configured_node_id, node_execution, require_local_router
from .routeros import RouterError, RouterOS, probe_key, quote


def addressing(router_id):
    if not 1 <= router_id <= 4095:
        raise RouterError('La reserva de direcciones del instalador admite IDs de router 1–4095.')
    network = ipaddress.ip_network('10.253.0.0/16')
    host = int(network.network_address) + router_id * 4
    return {'server': str(ipaddress.ip_address(host + 1)), 'router': str(ipaddress.ip_address(host + 2)), 'prefix': 30, 'server_port': 50000 + router_id, 'router_port': 55000 + router_id, 'tunnel_id': router_id, 'pool_start': str(ipaddress.ip_address(int(ipaddress.ip_address('10.254.0.0')) + router_id * 8 + 2)), 'pool_end': str(ipaddress.ip_address(int(ipaddress.ip_address('10.254.0.0')) + router_id * 8 + 6)), 'gateway': str(ipaddress.ip_address(int(ipaddress.ip_address('10.254.0.0')) + router_id * 8 + 1))}


def snapshot_hash(snapshot):
    snapshot = copy.deepcopy(snapshot)
    for peer in snapshot.get('peers', []):
        for key in ('last-handshake', 'rx', 'tx', 'current-endpoint-address', 'current-endpoint-port'):
            peer.pop(key, None)
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def build_plan(router):
    if not router.discovered_at or not router.trusted_host_key:
        raise RouterError('Primero complete la confianza SSH y el descubrimiento.')
    if router.provisioned_at:
        raise RouterError('El laboratorio ya está configurado. Verifique o revierta el trabajo existente antes de crear otro plan.')
    if not router.is_lab:
        raise RouterError('Este aprovisionador inicial solo aplica laboratorios aislados; el descubrimiento está disponible para routers de producción.')
    cfg = addressing(router.pk)
    endpoint = router.network_node.public_endpoint or (os.environ.get('NETWORK_PUBLIC_ENDPOINT', '') if router.network_node_id == 'primary' else '')
    try:
        ipaddress.IPv4Address(endpoint)
    except ValueError as exc:
        raise RouterError('El instalador debe definir NETWORK_PUBLIC_ENDPOINT con la IPv4 del servidor.') from exc
    for address in router.snapshot.get('addresses', []):
        existing = ipaddress.ip_interface(address['address'])
        if existing.network.overlaps(ipaddress.ip_interface(f'{cfg["router"]}/30').network) and address.get('interface') != f'{router.prefix}wg':
            raise RouterError('La subred privada propuesta coincide con configuración existente.')
    original_ppp = router.snapshot.get('ppp_aaa', {})
    original_incoming = router.snapshot.get('radius_incoming', {})
    globals_changed = original_ppp.get('use-radius') != 'yes' or original_ppp.get('accounting') != 'yes' or original_incoming.get('accept') != 'yes'
    return {'version': 1, 'network_node_id': router.network_node_id, 'snapshot_hash': router.snapshot_hash, 'router_id': router.pk, 'endpoint': endpoint, 'addressing': cfg, 'service_name': router.service_name, 'requires_global_approval': globals_changed, 'original_ppp_aaa': original_ppp, 'original_radius_incoming': original_incoming, 'changes': [f'Crear WireGuard {router.prefix}wg ({cfg["router"]}/30) con par exclusivo al servidor {cfg["server"]}.', f'Permitir UDP {cfg["router_port"]} solamente desde {endpoint}; permitir ICMP/GRE y desconexión RADIUS solo por el túnel privado.', f'Crear puente aislado {router.prefix}lab y EoIP sobre WireGuard; no agregar ether1 ni otros puertos existentes.', f'Crear perfil/pool propios y servicio PPPoE {router.service_name}; autenticación PAP/CHAP sobre el laboratorio aislado.', 'Agregar RADIUS exclusivo del servicio PPPoE nuevo, antes de los servidores anteriores; conservar sus secretos y ajustes.', 'Habilitar autenticación/contabilidad PPP y recepción de desconexiones solo si aún están desactivadas. Estos ajustes son globales.', 'Preparar WireGuard del servidor y cliente RADIUS mediante el agente restringido.'], 'untested': ['Sesión PPPoE real', 'Asignación de IP y velocidad', 'Contabilidad Start/Interim/Stop', 'Desconexión y reconexión']}


def enqueue(router, action, actor=None, plan=None, key=None):
    if action not in dict(ProvisioningJob.ACTIONS):
        raise RouterError('Operación no admitida.')
    if action in {'apply', 'rollback', 'lab', 'disconnect'} and not plan:
        raise RouterError('La operación requiere un plan revisado.')
    key = key or f'{router.pk}:{action}:{secrets.token_hex(16)}'
    job, created = ProvisioningJob.objects.get_or_create(idempotency_key=key, defaults={'router': router, 'action': action, 'actor': actor, 'plan': plan or {}, 'approved_at': timezone.now() if action in {'apply', 'rollback', 'lab', 'disconnect'} else None})
    if created:
        audit(actor, f'network.{action}.queued', router, {'job': str(job.pk)})
    return job


def journal(job, step):
    check_current_lease()
    job.journal = [*job.journal, step]
    job.save(update_fields=['journal'])


def mark_readiness(router, **values):
    check_current_lease()
    router.readiness = {**router.readiness, **values}
    router.save(update_fields=['readiness'])


def apply_plan(job, api, agent=call_agent):
    router, plan = job.router, job.plan
    require_local_router(router)
    if plan.get('network_node_id', 'primary') != router.network_node_id:
        raise RouterError('El plan pertenece a otro nodo; vuelva a descubrir y revisar la ubicación de red.')
    endpoint = router.network_node.public_endpoint or (os.environ.get('NETWORK_PUBLIC_ENDPOINT', '') if router.network_node_id == 'primary' else '')
    if plan.get('endpoint') != endpoint:
        raise RouterError('El endpoint del nodo cambió; revise y apruebe un plan nuevo.')
    if not job.approved_at or plan.get('router_id') != router.pk:
        raise RouterError('Falta aprobación válida del plan.')
    if not job.journal:
        current = api.discover()
        if snapshot_hash(current) != plan.get('snapshot_hash'):
            raise RouterError('La configuración cambió después de la revisión. Descubra y revise un plan nuevo.')
        if plan.get('requires_global_approval') and not plan.get('global_approved'):
            raise RouterError('Falta la aprobación explícita de ajustes globales PPP/RADIUS.')
    cfg, prefix = plan['addressing'], router.prefix
    if not router.radius_secret_encrypted:
        router.radius_secret_encrypted = encrypt(secrets.token_urlsafe(32))
        router.save(update_fields=['radius_secret_encrypted'])
    prepared = agent('prepare', router.pk)
    if not any(x.get('kind') == 'host' for x in job.journal):
        journal(job, {'kind': 'host'})

    def create(menu, suffix, **values):
        marker = f'fireisp:{router.pk}:{suffix}'
        # Intent is durable before the remote mutation. Replay adopts only this exact owned marker.
        if not any(x.get('marker') == marker for x in job.journal):
            journal(job, {'kind': 'resource', 'menu': menu, 'marker': marker})
        if 'place-before' in values and not api.rows(menu, ['comment']):
            values.pop('place-before')
        api.create(menu, marker, values)

    create('/interface/wireguard', 'wg', name=f'{prefix}wg', **{'listen-port': cfg['router_port'], 'mtu': 1380})
    wg = next(row for row in api.rows('/interface/wireguard', ['name', 'public-key']) if row['name'] == f'{prefix}wg')
    agent('configure', router.pk, peer_public_key=wg['public-key'], remote_endpoint=router.management_host, radius_secret=decrypt(router.radius_secret_encrypted))
    create('/ip/address', 'wg-address', address=f'{cfg["router"]}/30', interface=f'{prefix}wg')
    create('/interface/wireguard/peers', 'wg-peer', interface=f'{prefix}wg', **{'public-key': prepared['public_key'], 'allowed-address': f'{cfg["server"]}/32', 'endpoint-address': plan['endpoint'], 'endpoint-port': cfg['server_port'], 'persistent-keepalive': 25})
    create('/ip/firewall/filter', 'wg-input', chain='input', action='accept', protocol='udp', **{'src-address': plan['endpoint'], 'dst-port': cfg['router_port'], 'place-before': 0})
    for protocol in ('icmp', 'gre'):
        create('/ip/firewall/filter', f'private-{protocol}', chain='input', action='accept', protocol=protocol, **{'src-address': f'{cfg["server"]}/32', 'in-interface': f'{prefix}wg', 'place-before': 0})
    create('/ip/firewall/filter', 'private-disconnect', chain='input', action='accept', protocol='udp', **{'src-address': f'{cfg["server"]}/32', 'in-interface': f'{prefix}wg', 'dst-port': int(plan['original_radius_incoming'].get('port', 1700)), 'place-before': 0})
    create('/ip/firewall/filter', 'ppp-test-ping', chain='input', action='accept', protocol='icmp', **{'src-address': str(ipaddress.ip_interface(cfg['gateway'] + '/29').network), 'dst-address': cfg['gateway'], 'place-before': 0})
    create('/interface/bridge', 'bridge', name=f'{prefix}lab', **{'protocol-mode': 'none'})
    create('/interface/eoip', 'eoip', name=f'{prefix}eoip', **{'local-address': cfg['router'], 'remote-address': cfg['server'], 'tunnel-id': cfg['tunnel_id'], 'mtu': 1300, 'allow-fast-path': 'no'})
    create('/interface/bridge/port', 'bridge-eoip', bridge=f'{prefix}lab', interface=f'{prefix}eoip')
    create('/ip/pool', 'pool', name=f'{prefix}pool', ranges=f'{cfg["pool_start"]}-{cfg["pool_end"]}')
    create('/ppp/profile', 'profile', name=f'{prefix}profile', **{'local-address': cfg['gateway'], 'remote-address': f'{prefix}pool', 'only-one': 'yes', 'change-tcp-mss': 'yes', 'rate-limit': '5M/10M'})
    radius_values = {'address': cfg['server'], 'src-address': cfg['router'], 'service': 'ppp', 'called-id': router.service_name, 'secret': decrypt(router.radius_secret_encrypted), 'timeout': '3s', 'require-message-auth': 'yes-for-request-resp'}
    if api.rows('/radius', ['address']):
        radius_values['place-before'] = 0
    create('/radius', 'radius', **radius_values)
    create('/interface/pppoe-server/server', 'pppoe', interface=f'{prefix}lab', **{'service-name': router.service_name, 'default-profile': f'{prefix}profile', 'authentication': 'pap,chap', 'max-mtu': 1280, 'max-mru': 1280, 'one-session-per-host': 'yes', 'disabled': 'no'})
    for menu, before, after in [('/ppp/aaa', plan['original_ppp_aaa'], {'use-radius': 'yes', 'accounting': 'yes'}), ('/radius/incoming', plan['original_radius_incoming'], {'accept': 'yes'})]:
        changes = {key: value for key, value in after.items() if before.get(key) != value}
        if changes:
            if not any(x.get('kind') == 'global' and x['menu'] == menu for x in job.journal):
                journal(job, {'kind': 'global', 'menu': menu, 'before': {k: before[k] for k in changes}, 'after': changes})
            api.run(f'{menu} set ' + ' '.join(f'{k}={quote(v)}' for k, v in changes.items()))
    router.provisioned_at = timezone.now()
    router.save(update_fields=['provisioned_at'])
    mark_readiness(router, configured=True, private_link=False, pppoe_session=False, accounting=False, disconnect=False, reconnect=False)
    return {'configured': True, 'private_link': False, 'pppoe_session': False, 'message': 'Configuración creada. Ejecute verificación y prueba PPPoE; aún no está validada de extremo a extremo.'}


def rollback(job, api, agent=call_agent):
    source = ProvisioningJob.objects.get(pk=job.plan['source_job'], router=job.router, action='apply')
    warnings = []
    for step in reversed(source.journal):
        if step['kind'] == 'resource':
            api.remove_managed(step['menu'], step['marker'])
        elif step['kind'] == 'global':
            current = api.settings(step['menu'], list(step['after']))
            if current == step['after']:
                api.run(f'{step["menu"]} set ' + ' '.join(f'{k}={quote(v)}' for k, v in step['before'].items()))
            else:
                warnings.append(f'{step["menu"]}: cambió desde la aplicación; se conservó el valor actual.')
        elif step['kind'] == 'host':
            agent('remove', job.router_id)
    job.router.provisioned_at = None
    job.router.readiness = {}
    job.router.save(update_fields=['provisioned_at', 'readiness'])
    return {'rolled_back': True, 'warnings': warnings}


def verify(job, api, agent=call_agent):
    cfg = addressing(job.router_id)
    host = agent('status', job.router_id)
    ping = api.run(f'/ping address={quote(cfg["server"])} src-address={quote(cfg["router"])} count=3')
    linked = bool(host.get('handshake_recent')) and ('received=3' in ping or '3 packets received' in ping or 'packet-loss=0%' in ping)
    source = job.router.jobs.filter(action='apply').first()
    missing = [step['marker'] for step in (source.journal if source else []) if step.get('kind') == 'resource' and not api.managed_exists(step['menu'], step['marker'])]
    if missing and source:
        source.status = 'failed'
        source.error = 'La verificación detectó recursos propios ausentes. Reintente este plan aprobado para completarlos.'
        source.save(update_fields=['status', 'error'])
        job.router.provisioned_at = None
        job.router.save(update_fields=['provisioned_at'])
    mark_readiness(job.router, private_link=linked, configured=not missing)
    return {'host': host, 'configured': not missing, 'missing_managed_resources': missing, 'private_link': linked, 'pppoe_session': False, 'message': 'El enlace privado no sustituye una sesión PPPoE real.'}


def queue_rates(value):
    """Normalize RouterOS JSON arrays and CLI unit strings into upload/download bps."""
    values = value if isinstance(value, (list, tuple)) else str(value).split('/')
    if len(values) != 2:
        return None
    rates = []
    for item in values:
        match = re.fullmatch(r'(\d+(?:\.\d+)?)([kKmMgG]?)', str(item).strip())
        if not match:
            return None
        rates.append(int(float(match[1]) * {'': 1, 'k': 1000, 'm': 1000000, 'g': 1000000000}[match[2].lower()]))
    return rates


def lab_queue_rates(api, username):
    return [queue_rates(row.get('max-limit')) for row in api.rows('/queue/simple', ['name', 'max-limit']) if username in row.get('name', '')]


def lab_test(job, api, agent=call_agent):
    router = job.router
    if not router.is_lab or not router.provisioned_at or not router.organization.demo_mode:
        raise RouterError('Esta prueba requiere el laboratorio aislado aprovisionado por FireISP.')
    from core.models import Customer, Plan, Subscription
    from .agent_client import AgentError
    username = f'{router.prefix}-lab-{secrets.token_hex(4)}'
    password = secrets.token_urlsafe(24)
    # An explicit demo-only service exercises the same suspension/plan state as subscribers.
    # activated_at remains NULL throughout, so this network test never starts billing.
    customer = Customer.objects.create(organization=router.organization, code=username, name='Prueba técnica PPPoE ' + username, address='Laboratorio aislado EoIP', is_active=False)
    plan = Plan.objects.create(organization=router.organization, name='Prueba técnica ' + username, download_mbps=10, upload_mbps=5, price_mxn='1.00', is_active=False)
    subscription = Subscription.objects.create(customer=customer, plan=plan, status='active', access_username=username)
    credential = RadiusCredential.objects.create(router=router, subscription=subscription, username=username, password_encrypted=encrypt(password), is_lab=True, expires_at=timezone.now() + timedelta(minutes=10))
    result = {}
    try:
        sync_confirmed_entitlements(agent)
        result = agent('lab_start', router.pk, username=username, password=password, service_name=router.service_name)
        active = [s for s in api.rows('/ppp/active', ['name', 'service', 'address', 'session-id']) if s.get('name') == username]
        session_ok = bool(active) and result.get('ppp_up', False)
        observed_rates = lab_queue_rates(api, username)
        speed_ok = [5000000, 10000000] in observed_rates
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not RadiusSession.objects.filter(router=router, username=username, started_at__isnull=False).exists():
            time.sleep(1)
        accounting_start = RadiusSession.objects.filter(router=router, username=username, started_at__isnull=False).exists()
        accounting_interim = None
        if job.plan.get('wait_interim'):
            accounting_interim = False
            deadline = time.monotonic() + 75
            while time.monotonic() < deadline:
                session = RadiusSession.objects.filter(router=router, username=username, started_at__isnull=False, stopped_at__isnull=True).first()
                if session and session.updated_at >= session.started_at + timedelta(seconds=30):
                    accounting_interim = True
                    break
                time.sleep(2)
        subscription.status = 'suspended'
        subscription.save(update_fields=['status'])
        queued = queue_subscription_sync(subscription.pk, job.actor)
        disconnect_task = ProvisioningJob.objects.get(pk=queued['jobs'][0])
        # Use the real reconciliation service, including cache update and authenticated RADIUS DM.
        process_job(disconnect_task)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            disconnect_task.refresh_from_db()
            if disconnect_task.status != 'running':
                break
            time.sleep(1)
        disconnected = disconnect_task.status == 'succeeded' and disconnect_task.result.get('sessions_terminated', 0) > 0 and not any(s.get('name') == username for s in api.rows('/ppp/active', ['name']))
        agent('lab_stop', router.pk)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not RadiusSession.objects.filter(router=router, username=username, stopped_at__isnull=False).exists():
            time.sleep(1)
        accounting_stop = RadiusSession.objects.filter(router=router, username=username, stopped_at__isnull=False).exists()
        rejected = False
        try:
            agent('lab_start', router.pk, username=username, password=password, service_name=router.service_name)
        except AgentError as exc:
            rejected = 'autenticación RADIUS rechazada' in str(exc) and not any(s.get('name') == username for s in api.rows('/ppp/active', ['name']))
        finally:
            agent('lab_stop', router.pk)
        plan.download_mbps = 20
        plan.save(update_fields=['download_mbps'])
        subscription.status = 'active'
        subscription.save(update_fields=['status'])
        queue_subscription_sync(subscription.pk, job.actor)
        sync_confirmed_entitlements(agent)
        reconnect_result = agent('lab_start', router.pk, username=username, password=password, service_name=router.service_name)
        reconnected = reconnect_result.get('ppp_up', False) and any(s.get('name') == username for s in api.rows('/ppp/active', ['name']))
        changed_rates = lab_queue_rates(api, username)
        plan_changed = [5000000, 20000000] in changed_rates
        result.update(pppoe_session=session_ok, rate_limit=speed_ok, observed_rates_bps=observed_rates, changed_rates_bps=changed_rates, plan_change=plan_changed, accounting_start=accounting_start, accounting_interim=accounting_interim, accounting_stop=accounting_stop, disconnect=disconnected, suspension_reject=rejected, reconnect=reconnected, billing_started=False, demo_subscription_id=subscription.pk)
        result['end_to_end'] = all(result.get(k) for k in ['pppoe_session', 'rate_limit', 'accounting_start', 'accounting_stop', 'disconnect', 'suspension_reject', 'reconnect', 'plan_change', 'gateway_ping'])
        if job.plan.get('wait_interim'):
            result['end_to_end'] = result['end_to_end'] and accounting_interim
        mark_readiness(router, pppoe_session=session_ok, rate_limit=speed_ok, accounting=accounting_start and accounting_stop, disconnect=disconnected, suspension_reject=rejected, reconnect=reconnected, plan_change=plan_changed, end_to_end=result['end_to_end'])
        return result
    finally:
        try:
            agent('lab_stop', router.pk)
        finally:
            subscription.status = 'cancelled'
            subscription.save(update_fields=['status'])
            credential.enabled = False
            credential.save(update_fields=['enabled'])
            sync_confirmed_entitlements(agent)


def process_job(job, claimed=False):
    """Claim and execute under the node lock; callers cannot bypass ownership."""
    require_local_router(job.router)
    try:
        with node_execution() as lease:
            with transaction.atomic():
                accepted = ProvisioningJob.objects.select_for_update().filter(pk=job.pk, status='pending', router__execution_blocked=False).first()
                if not accepted:
                    job.refresh_from_db()
                    return job
                job.refresh_from_db()
                job.status = 'running'
                job.started_at = timezone.now()
                job.attempts += 1
                job.error = ''
                job.worker_token = lease.token
                job.worker_generation = lease.generation
                job.save(update_fields=['status', 'started_at', 'attempts', 'error', 'worker_token', 'worker_generation'])
            return _execute_job(job, lease)
    except NodeBusy:
        job.refresh_from_db()
        return job


def _execute_job(job, lease):
    try:
        lease.check()
        if job.action == 'probe':
            job.router.candidate_host_key = probe_key(job.router)
            job.router.save(update_fields=['candidate_host_key'])
            result = {'message': 'Huella obtenida sin enviar credenciales. Revísela antes de confiar.'}
        else:
            with RouterOS(job.router) as api:
                if job.action == 'discover':
                    snapshot = api.discover()
                    job.router.snapshot = snapshot
                    job.router.snapshot_hash = snapshot_hash(snapshot)
                    job.router.discovered_at = timezone.now()
                    job.router.save(update_fields=['snapshot', 'snapshot_hash', 'discovered_at'])
                    result = {'discovered': True, 'active_sessions': len(snapshot.get('active', [])), 'version': snapshot.get('resource', {}).get('version')}
                elif job.action == 'apply':
                    result = apply_plan(job, api)
                elif job.action == 'rollback':
                    result = rollback(job, api)
                elif job.action == 'verify':
                    result = verify(job, api)
                elif job.action == 'lab':
                    if job.plan.get('test_kind') == 'web-outage-fallback':
                        raise RouterError('La prueba de caída requiere el comando network_lab_fallback con prepare/test/cleanup; limpie el trabajo y prepare una prueba nueva.')
                    result = lab_test(job, api)
                elif job.action == 'disconnect':
                    result = disconnect_job(job, api)
                else:
                    raise RouterError('Operación no admitida.')
        lease.check()
        job.result = result
        job.status = 'succeeded'
    except Exception as exc:
        job.status = 'failed'
        # Known errors are authored by our adapters; never persist raw SSH/HTTP/OS exceptions.
        from .agent_client import AgentError
        job.error = str(exc) if isinstance(exc, (RouterError, AgentError)) else 'La operación falló. Revise conectividad, servicios y permisos; el trabajo conserva su registro para reintento o reversión.'
    # A superseded worker never commits a result under the replacement lease.
    lease.check()
    job.finished_at = timezone.now()
    updated = ProvisioningJob.objects.filter(pk=job.pk, status='running', worker_token=lease.token, worker_generation=lease.generation).update(status=job.status, result=job.result, error=job.error, finished_at=job.finished_at)
    if not updated:
        raise RouterError('La propiedad del trabajo cambió; conserve el resultado para revisión.')
    audit(job.actor, f'network.{job.action}.{job.status}', job.router, {'job': str(job.pk), 'node_id': lease.node.pk})
    return job


@transaction.atomic
def retry_reviewed_job(job, actor=None):
    """Operator review is required after uncertain external effects."""
    job = ProvisioningJob.objects.select_for_update().get(pk=job.pk)
    if job.status != 'failed':
        raise RouterError('Solo se puede reintentar un trabajo fallido.')
    node = NetworkNode.objects.select_for_update().get(pk=job.router.network_node_id)
    if node.worker_token and node.lease_expires_at and node.lease_expires_at > timezone.now():
        raise RouterError('Espere a que termine la ejecución activa del nodo antes de reintentar.')
    if job.router.jobs.filter(status='running').exists():
        raise RouterError('Hay una ejecución pendiente de revisión para este router.')
    job.status = 'pending'
    job.worker_token = None
    job.save(update_fields=['status', 'worker_token'])
    Router.objects.filter(pk=job.router_id).update(execution_blocked=False)
    audit(actor, 'network.job.retry', job.router, {'job': str(job.pk), 'reviewed_after_interruption': True})
    return job


def configure_subscription(subscription, router, password, actor=None):
    """Store access credentials; activation remains a separate business workflow."""
    if subscription.customer.organization_id != router.organization_id:
        raise RouterError('El router y el cliente pertenecen a organizaciones distintas.')
    if not 12 <= len(password) <= 128:
        raise RouterError('La contraseña PPPoE debe contener de 12 a 128 caracteres.')
    credential, _ = RadiusCredential.objects.update_or_create(subscription=subscription, defaults={'router': router, 'username': subscription.access_username, 'password_encrypted': encrypt(password), 'enabled': subscription.status == 'active', 'is_lab': False, 'commissioning': False, 'expires_at': None})
    audit(actor, 'network.subscription.credentials', subscription, {'router_id': router.pk})
    return credential


@transaction.atomic
def queue_subscription_sync(subscription_id, actor=None):
    """Idempotent desired-state reconciliation for billing/cancellation outbox handlers."""
    from core.models import Subscription
    subscription = Subscription.objects.select_for_update().get(pk=subscription_id)
    credential = RadiusCredential.objects.select_for_update().filter(subscription=subscription).first()
    if not credential:
        return {'configured': False, 'disconnect_pending': subscription.status != 'active'}
    enabled = subscription.status == 'active'
    if credential.enabled != enabled:
        credential.state_revision += 1
        credential.enabled = enabled
        credential.save(update_fields=['enabled', 'state_revision'])
    jobs = []
    if not enabled:
        job = enqueue(credential.router, 'disconnect', actor, {'credential_id': credential.pk, 'reason': subscription.status}, key=f'subscription:{subscription.pk}:{subscription.status}:{credential.state_revision}:reconcile')
        jobs.append(str(job.pk))
    return {'configured': True, 'enabled': enabled, 'jobs': jobs, 'disconnect_pending': bool(jobs)}



def queue_plan_change(subscription_id, actor=None):
    """Disconnect only observed sessions; next authentication receives current plan."""
    credential = RadiusCredential.objects.filter(subscription_id=subscription_id).first()
    if not credential:
        return []
    jobs = []
    for session in RadiusSession.objects.filter(router=credential.router, username=credential.username, stopped_at__isnull=True):
        job = enqueue(credential.router, 'disconnect', actor, {'session_id': session.pk, 'reason': 'plan-change'}, key=f'plan-change:{subscription_id}:{session.pk}')
        jobs.append(str(job.pk))
    return jobs


def sync_confirmed_entitlements(agent=call_agent):
    """Serialize whole-node snapshots with provisioning; nodes never share caches."""
    with node_execution() as lease:
        return _sync_confirmed_entitlements(agent, lease)


def _sync_confirmed_entitlements(agent, lease):
    """Publish confirmed desired state; never derive automatic suspension from elapsed time."""
    entries = []
    now = timezone.now()
    credentials = RadiusCredential.objects.filter(enabled=True, router__provisioned_at__isnull=False, router__network_node_id=configured_node_id()).select_related('router', 'subscription__plan', 'subscription__customer').order_by('pk')
    for credential in credentials.iterator(chunk_size=500):
        if credential.expires_at and credential.expires_at <= now:
            continue
        download, upload = credential.download_mbps, credential.upload_mbps
        if not credential.is_lab or credential.subscription_id:
            sub = credential.subscription
            commissioning = credential.commissioning and credential.expires_at and credential.expires_at > now and sub and sub.status == 'pending'
            if not sub or (sub.status != 'active' and not commissioning) or sub.customer.organization_id != credential.router.organization_id:
                continue
            download, upload = sub.plan.download_mbps, sub.plan.upload_mbps
        if len(entries) >= MAX_ENTITLEMENT_ENTRIES:
            raise AgentError('La instantánea excede el límite de abonados; se conserva la anterior.')
        entries.append({'username': credential.username, 'password': decrypt(credential.password_encrypted), 'router_id': credential.router_id, 'upload_mbps': upload, 'download_mbps': download, 'expires_at': credential.expires_at.isoformat() if credential.expires_at else None})
    # The agent writes one atomic complete snapshot. On failure the previous generation remains.
    lease.check()
    result = agent('sync_entitlements', 1, entries=entries)
    lease.check()
    return {**result, 'network_node_id': lease.node.pk, 'generation': lease.generation}


def disconnect_job(job, api, agent=call_agent):
    credential = None
    if job.plan.get('credential_id'):
        credential = RadiusCredential.objects.select_related('subscription').get(pk=job.plan['credential_id'], router=job.router)
        username = credential.username
        if credential.subscription and job.plan.get('reason') in {'pending', 'suspended', 'cancelled'} and credential.subscription.status != job.plan['reason']:
            return {'superseded': True, 'router_observed': False, 'sessions_terminated': 0}
    else:
        session = RadiusSession.objects.get(pk=job.plan['session_id'], router=job.router)
        username = session.username
    # Confirm desired authorization in the independent RADIUS store before terminating.
    sync_confirmed_entitlements(agent)
    active = [row for row in api.rows('/ppp/active', ['name', 'session-id']) if row.get('name') == username]
    for row in active:
        observed = RadiusSession.objects.filter(router=job.router, username=username, stopped_at__isnull=True).order_by('-started_at').first()
        session_id = observed.session_id if observed else row.get('session-id', '').removeprefix('0x')
        if not session_id:
            raise RouterError('La sesión activa no tiene identificador RADIUS; se necesita revisión del NAS.')
        response = agent('disconnect', job.router_id, username=username, session_id=session_id, port=int(job.router.snapshot.get('radius_incoming', {}).get('port', 1700)))
        if not response.get('ack'):
            raise RouterError('El router no confirmó la desconexión RADIUS. El acceso nuevo sigue deshabilitado; reintente la sesión existente.')
    if active:
        time.sleep(2)
    if any(row.get('name') == username for row in api.rows('/ppp/active', ['name'])):
        raise RouterError('El router todavía informa una sesión activa después de la desconexión.')
    if credential and credential.subscription and credential.subscription.status == 'cancelled':
        from compliance.models import CancellationRequest
        CancellationRequest.objects.filter(subscription=credential.subscription).update(network_disconnect_pending=False)
    return {'disconnected': True, 'router_observed': True, 'sessions_terminated': len(active)}
