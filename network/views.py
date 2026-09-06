import json
import os
import secrets
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from core.secrets import decrypt
from core.security import staff_required
from core.services import audit
from .forms import ReviewForm, RouterForm, TrustForm
from .models import ProvisioningJob, RadiusCredential, RadiusSession, Router
from .routeros import RouterError, fingerprint
from .services import addressing, build_plan, enqueue


@staff_required
def router_list(request):
    return render(request, 'network/list.html', {'routers': Router.objects.all().order_by('name'), 'jobs': ProvisioningJob.objects.select_related('router')[:10]})


@staff_required
def router_create(request):
    form = RouterForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        router = form.save()
        enqueue(router, 'probe', request.user)
        return redirect('network:detail', pk=router.pk)
    return render(request, 'form.html', {'form': form, 'title': 'Agregar router'})


@staff_required
def router_detail(request, pk):
    router = get_object_or_404(Router, pk=pk)
    candidate = fingerprint(router.candidate_host_key)
    trusted = fingerprint(router.trusted_host_key)
    return render(request, 'network/detail.html', {'router': router, 'candidate': candidate, 'trusted': trusted, 'trust_form': TrustForm(initial={'fingerprint': candidate}), 'jobs': router.jobs.all()[:30], 'sessions': RadiusSession.objects.filter(router=router).order_by('-updated_at')[:20], 'snapshot_json': json.dumps(router.snapshot, indent=2, ensure_ascii=False)})


@staff_required
@require_POST
def trust_host(request, pk):
    router = get_object_or_404(Router, pk=pk)
    form = TrustForm(request.POST)
    if form.is_valid() and router.candidate_host_key and form.cleaned_data['fingerprint'] == fingerprint(router.candidate_host_key):
        router.trusted_host_key = router.candidate_host_key
        router.trusted_at = timezone.now()
        router.save(update_fields=['trusted_host_key', 'trusted_at'])
        audit(request.user, 'network.ssh.trust', router, {'fingerprint': fingerprint(router.trusted_host_key)})
        enqueue(router, 'discover', request.user)
        messages.success(request, 'Identidad SSH registrada; descubrimiento en cola.')
    else:
        messages.error(request, 'La huella no coincide o falta su confirmación. Revise la identidad nuevamente.')
    return redirect('network:detail', pk=pk)


@staff_required
def review(request, pk):
    router = get_object_or_404(Router, pk=pk)
    try:
        plan = build_plan(router)
    except RouterError as exc:
        messages.error(request, str(exc))
        return redirect('network:detail', pk=pk)
    form = ReviewForm(request.POST or None, initial={'snapshot_hash': router.snapshot_hash})
    if request.method == 'POST' and form.is_valid():
        if form.cleaned_data['snapshot_hash'] != router.snapshot_hash:
            form.add_error(None, 'El descubrimiento cambió; revise el plan actualizado.')
        elif plan['requires_global_approval'] and not form.cleaned_data['approve_global_ppp']:
            form.add_error('approve_global_ppp', 'Este plan necesita aprobación expresa de los ajustes globales indicados.')
        else:
            plan['global_approved'] = form.cleaned_data['approve_global_ppp']
            enqueue(router, 'apply', request.user, plan, key=f'{router.pk}:apply:{router.snapshot_hash}')
            messages.success(request, 'Plan aprobado y puesto en cola.')
            return redirect('network:detail', pk=pk)
    return render(request, 'network/review.html', {'router': router, 'plan': plan, 'form': form})


@staff_required
@require_POST
def action(request, pk, action):
    router = get_object_or_404(Router, pk=pk)
    if action not in {'probe', 'discover', 'verify', 'lab'}:
        return HttpResponse(status=400)
    if action == 'lab' and request.POST.get('confirm') != 'yes':
        return HttpResponse('Debe confirmar la prueba de laboratorio.', status=400)
    enqueue(router, action, request.user, {'router_id': pk, 'isolated_lab_approved': True} if action == 'lab' else None)
    messages.success(request, 'Trabajo en cola; la página mostrará su resultado al actualizar.')
    return redirect('network:detail', pk=pk)


@staff_required
@require_POST
def retry_job(request, job_id):
    job = get_object_or_404(ProvisioningJob, pk=job_id)
    if job.status != 'failed':
        return HttpResponse(status=409)
    job.status = 'pending'
    job.save(update_fields=['status'])
    audit(request.user, 'network.job.retry', job.router, {'job': str(job.pk)})
    return redirect('network:detail', pk=job.router_id)


@staff_required
@require_POST
def rollback_job(request, job_id):
    source = get_object_or_404(ProvisioningJob, pk=job_id, action='apply')
    if source.status == 'running' or request.POST.get('confirm') != 'yes':
        return HttpResponse(status=409)
    enqueue(source.router, 'rollback', request.user, {'source_job': str(source.pk)}, key=f'rollback:{source.pk}')
    return redirect('network:detail', pk=source.router_id)


def radius_payload(request):
    token = getattr(settings, 'NETWORK_RADIUS_TOKEN', '') or os.environ.get('NETWORK_RADIUS_TOKEN', '')
    provided = request.headers.get('Authorization', '')
    if len(token) < 32 or not secrets.compare_digest(provided.encode(), ('Bearer ' + token).encode()):
        return None, JsonResponse({'error': 'unauthorized'}, status=401)
    if request.method != 'POST' or request.content_type != 'application/json' or len(request.body) > 262144:
        return None, JsonResponse({'error': 'invalid request'}, status=400)
    try:
        data = json.loads(request.body)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return None, JsonResponse({'error': 'invalid request'}, status=400)
    return data, None


def attr(data, name, default=''):
    value = data.get(name, default)
    if isinstance(value, dict):
        value = value.get('value', default)
    if isinstance(value, list):
        value = value[0] if value else default
    return str(value)


def radius_router(data):
    source = attr(data, 'Packet-Src-IP-Address') or attr(data, 'NAS-IP-Address')
    for router in Router.objects.filter(provisioned_at__isnull=False):
        if addressing(router.pk)['router'] == source:
            return router
    return None


@csrf_exempt
def radius_authorize(request):
    data, error = radius_payload(request)
    if error:
        return error
    router = radius_router(data)
    username = attr(data, 'User-Name')
    credential = RadiusCredential.objects.select_related('subscription__plan', 'subscription__customer').filter(router=router, username=username, enabled=True).first() if router else None
    if not credential or (credential.expires_at and credential.expires_at <= timezone.now()):
        return JsonResponse({'error': 'denied'}, status=403)
    download, upload = credential.download_mbps, credential.upload_mbps
    if not credential.is_lab or credential.subscription_id:
        subscription = credential.subscription
        commissioning = credential.commissioning and credential.expires_at and credential.expires_at > timezone.now() and subscription and subscription.status == 'pending'
        if not subscription or (subscription.status != 'active' and not commissioning) or subscription.customer.organization_id != router.organization_id:
            return JsonResponse({'error': 'denied'}, status=403)
        # Billing owns grace periods, dispute holds and outage freezes.
        # Authorization follows confirmed status, not a second clock-based cutoff.
        download, upload = subscription.plan.download_mbps, subscription.plan.upload_mbps
    response = JsonResponse({'control:Cleartext-Password': {'value': [decrypt(credential.password_encrypted)], 'op': ':='}, 'reply:Mikrotik-Rate-Limit': {'value': [f'{upload}M/{download}M'], 'op': ':='}, 'reply:Acct-Interim-Interval': {'value': [60], 'op': ':='}})
    response['Cache-Control'] = 'no-store'
    return response


@csrf_exempt
def radius_accounting(request):
    data, error = radius_payload(request)
    if error:
        return error
    router = radius_router(data)
    status = attr(data, 'Acct-Status-Type')
    if router and status in {'Accounting-On', 'Accounting-Off', '7', '8'}:
        return HttpResponse(status=204)
    session_id, username = attr(data, 'Acct-Session-Id'), attr(data, 'User-Name')
    if not router or not session_id or not username or len(session_id) > 128 or len(username) > 100:
        return JsonResponse({'error': 'invalid session'}, status=400)
    if status not in {'Start', 'Stop', 'Interim-Update', '1', '2', '3'}:
        return HttpResponse(status=204)
    try:
        inputs = int(attr(data, 'Acct-Input-Octets', '0')) + int(attr(data, 'Acct-Input-Gigawords', '0')) * (2 ** 32)
        outputs = int(attr(data, 'Acct-Output-Octets', '0')) + int(attr(data, 'Acct-Output-Gigawords', '0')) * (2 ** 32)
        if min(inputs, outputs) < 0 or max(inputs, outputs) > 2 ** 63 - 1:
            raise ValueError
        now = timezone.now()
        delay = int(attr(data, 'Acct-Delay-Time', '0'))
        if not 0 <= delay <= 2 ** 32 - 1:
            raise ValueError
        journal_timestamp = attr(data, 'FireISP-Journal-Timestamp')
        received_at = datetime.fromtimestamp(int(journal_timestamp), tz=datetime_timezone.utc) if journal_timestamp else now
        event_at = received_at - timedelta(seconds=delay)
        if event_at > now + timedelta(minutes=5) or event_at.year < 2000:
            raise ValueError
        event_at = min(event_at, now)
    except (ValueError, OverflowError, OSError):
        return JsonResponse({'error': 'invalid counters or timestamp'}, status=400)
    with transaction.atomic():
        session, created = RadiusSession.objects.select_for_update().get_or_create(router=router, session_id=session_id, defaults={'username': username})
        if session.username != username:
            return JsonResponse({'error': 'session conflict'}, status=409)
        last_event_at = event_at if created else max(session.updated_at, event_at)
        if status in {'Start', '1'}:
            session.started_at = min(session.started_at, event_at) if session.started_at else event_at
        if status in {'Stop', '2'}:
            session.stopped_at = min(session.stopped_at, event_at) if session.stopped_at else event_at
        framed_ip = attr(data, 'Framed-IP-Address')
        if framed_ip:
            import ipaddress
            try:
                session.framed_ip = str(ipaddress.ip_address(framed_ip))
            except ValueError:
                return JsonResponse({'error': 'invalid IP'}, status=400)
        session.input_octets = max(session.input_octets, inputs)
        session.output_octets = max(session.output_octets, outputs)
        session.terminate_cause = attr(data, 'Acct-Terminate-Cause')[:100] or session.terminate_cause
        if journal_timestamp:
            session.replayed_at = now
        session.save()
        # Receipt of an old journal entry must not manufacture recent delivery evidence.
        RadiusSession.objects.filter(pk=session.pk).update(updated_at=last_event_at)
    return HttpResponse(status=204)


@staff_required
def subscriber_access(request):
    from .forms import SubscriptionAccessForm
    from .services import configure_subscription, queue_plan_change
    from datetime import timedelta
    form = SubscriptionAccessForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        subscription = form.cleaned_data['subscription']
        try:
            if form.cleaned_data['commissioning'] and subscription.status != 'pending':
                raise RouterError('La puesta en servicio temporal solo se permite en una suscripción pendiente.')
            credential = configure_subscription(subscription, form.cleaned_data['router'], form.cleaned_data['password'], request.user)
            if form.cleaned_data['commissioning']:
                credential.commissioning = True
                credential.expires_at = timezone.now() + timedelta(hours=2)
                credential.enabled = True
                credential.save(update_fields=['commissioning', 'expires_at', 'enabled'])
            if form.cleaned_data['disconnect_current']:
                queue_plan_change(subscription.pk, request.user)
            messages.success(request, 'Acceso guardado. La suscripción conserva su estado de instalación y facturación; la sesión se verifica por contabilidad real.')
            return redirect('network:access')
        except RouterError as exc:
            form.add_error(None, str(exc))
    return render(request, 'network/access.html', {'form': form, 'credentials': RadiusCredential.objects.filter(is_lab=False).select_related('router', 'subscription__customer')})
