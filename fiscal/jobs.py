"""Database-backed fiscal work, safe across hosts and broker/worker interruptions."""
import hashlib
import logging
import uuid
from datetime import timedelta
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from core.services import audit
from .models import FiscalDocument, FiscalJob, FiscalProfile
from . import services

logger = logging.getLogger(__name__)
LEASE_SECONDS = 300  # Longer than the worker hard time limit (120 seconds).
MAX_ATTEMPTS = 3
MAX_PDF_BYTES = 8 * 1024 * 1024
ACTIVE = ('queued', 'running')


def _publish(job_id):
    try:
        from .tasks import process_fiscal_job
        process_fiscal_job.apply_async(args=[str(job_id)], queue='fiscal', retry=False)
        return True
    except Exception:
        # The committed row remains discoverable by the periodic dispatcher.
        logger.warning('Fiscal dispatch deferred; durable job %s remains pending.', job_id)
        return False


@transaction.atomic
def queue_job(operation, *, document=None, profile=None, actor=None, payload=None, request_key=None):
    payload = payload or {}
    if operation not in dict(FiscalJob.OPERATIONS):
        raise ValidationError('Operación fiscal no válida.')
    if operation == 'verify':
        if document is not None or profile is None or payload:
            raise ValidationError('Selecciona la conexión fiscal que deseas verificar.')
        target = FiscalProfile.objects.select_for_update().get(pk=profile.pk)
        selector = {'profile': target}
    else:
        if document is None or profile is not None:
            raise ValidationError('Selecciona un documento fiscal.')
        target = FiscalDocument.objects.select_for_update().get(pk=document.pk)
        selector = {'document': target}
        if operation == 'cancel':
            reason = payload.get('reason', '02')
            replacement = str(payload.get('replacement', ''))
            if set(payload) - {'reason', 'replacement'} or reason not in {'01', '02', '03', '04'}:
                raise ValidationError('Motivo de cancelación inválido.')
            if reason == '01':
                try:
                    uuid.UUID(replacement)
                except (ValueError, AttributeError):
                    raise ValidationError('El motivo 01 requiere el UUID sustituto.') from None
            payload = {'reason': reason, 'replacement': replacement}
        elif payload:
            raise ValidationError('La operación fiscal no acepta parámetros adicionales.')
    if request_key:
        existing = FiscalJob.objects.filter(request_key=request_key).first()
        if existing:
            if existing.operation != operation or existing.payload != payload or any(getattr(existing, key + '_id') != value.pk for key, value in selector.items()):
                raise ValidationError('El identificador ya corresponde a otra solicitud fiscal.')
            return existing
    existing = FiscalJob.objects.filter(**selector, status__in=ACTIVE).first()
    if existing:
        if existing.operation != operation or existing.payload != payload:
            raise ValidationError('Ya hay una operación pendiente para este documento. Espera su resultado.')
        return existing
    if operation == 'stamp' and target.status in {'submitting', 'uncertain'}:
        raise ValidationError('La operación puede estar timbrada. Solicita recuperar el mismo XML.')
    if operation in {'recover', 'pdf', 'cancellation_status'} and not (target.request_xml if operation == 'recover' else target.xml):
        raise ValidationError('El documento todavía no contiene el XML requerido.')
    if operation == 'cancel' and target.status not in {'stamped', 'cancel_pending', 'cancelled'}:
        raise ValidationError('Sólo puede cancelarse un documento timbrado.')
    job = FiscalJob.objects.create(operation=operation, actor=actor, payload=payload,
        request_key=request_key or str(uuid.uuid4()), **selector)
    audit(actor, 'fiscal.job.queued', job.pk, {'operation': operation, 'document_id': job.document_id})
    transaction.on_commit(lambda: _publish(job.pk))
    return job


def claim_job(job_id):
    with transaction.atomic():
        job = FiscalJob.objects.select_for_update().filter(pk=job_id).first()
        if not job or job.status != 'queued' or job.available_at > timezone.now():
            return None
        job.status = 'running'
        job.attempts += 1
        job.claim_token = uuid.uuid4()
        job.lease_until = timezone.now() + timedelta(seconds=LEASE_SECONDS)
        job.message = ''
        job.save(update_fields=['status', 'attempts', 'claim_token', 'lease_until', 'message'])
        return job


def _prepare_for_stamp(document, actor, claim=None):
    # Signing stays on the fiscal worker. Persist the exact signed XML before PAC I/O.
    with services.claim_transaction(claim):
        document = FiscalDocument.objects.select_for_update().get(pk=document.pk)
        if not document.request_xml:
            if document.status != 'draft':
                raise ValidationError('Revisa el documento antes de generar otro XML.')
            profile = services._profile(services.document_organization(document))
            document.request_xml = services.bounded_xml(services._build_cfdi(document, profile).xml_bytes()).decode()
            document.save(update_fields=['request_xml'])
            audit(actor, 'fiscal.document.signed', document.pk)
    return document


def pdf_ready(document):
    return bool(document.pdf_content) and document.pdf_source_sha256 == hashlib.sha256(document.xml.encode()).hexdigest()


def render_pdf(document, claim=None):
    services.bounded_xml(document.xml)
    if pdf_ready(document):
        return
    from satcfdi.cfdi import CFDI
    from satcfdi import render
    source = document.xml
    result = render.pdf_bytes(CFDI.from_string(source.encode()))
    if len(result) > MAX_PDF_BYTES or not result.startswith(b'%PDF-'):
        raise ValidationError('No fue posible preparar un PDF válido dentro del tamaño permitido.')
    # Store the artifact in PostgreSQL; a web node never needs this worker's disk.
    with services.claim_transaction(claim):
        FiscalDocument.objects.filter(pk=document.pk, xml=source).update(
            pdf_content=result, pdf_source_sha256=hashlib.sha256(source.encode()).hexdigest())


def run_job(job_id):
    job = claim_job(job_id)
    if job is None:
        return False
    status, message = 'succeeded', 'Operación completada.'
    try:
        document, actor = job.document, job.actor
        if job.operation == 'verify':
            ok, message = services.verify_credentials(job.profile, actor, claim=job)
            status = 'succeeded' if ok else 'failed'
        elif job.operation in {'stamp', 'recover'}:
            if job.operation == 'stamp':
                document = _prepare_for_stamp(document, actor, claim=job)
            services.stamp_document(document, actor, recover=job.operation == 'recover', claim=job)
            message = 'CFDI disponible. Se está preparando su PDF.'
        elif job.operation == 'cancel':
            services.cancel_document(document, actor, claim=job, **job.payload)
            message = 'Solicitud registrada. La cancelación requiere confirmación del SAT.'
        elif job.operation == 'cancellation_status':
            message = 'Estado informado por SAT DEMO: ' + services.refresh_cancellation(document, actor, claim=job)
        elif job.operation == 'pdf':
            render_pdf(document, claim=job)
            message = 'PDF disponible para descargar.'
    except Exception:
        # Never persist exceptions that may include credentials, SOAP or signed requests.
        status, message = 'failed', 'No se completó la operación. Revisa el documento o la configuración fiscal.'
        if job.document_id:
            document = FiscalDocument.objects.get(pk=job.document_id)
            if document.status in {'uncertain', 'submitting', 'cancel_pending'}:
                status, message = 'review', 'El resultado requiere consulta al PAC o al SAT. No se reenviará la solicitud.'
    with transaction.atomic():
        changed = FiscalJob.objects.filter(pk=job.pk, status='running', claim_token=job.claim_token, lease_until__gt=timezone.now()).update(
            status=status, message=message[:500], finished_at=timezone.now(), lease_until=None)
        if changed:
            audit(job.actor, 'fiscal.job.finished', job.pk, {'operation': job.operation, 'status': status})
    if changed and status == 'succeeded' and job.operation in {'stamp', 'recover'}:
        try:
            queue_job('pdf', document=FiscalDocument.objects.get(pk=job.document_id), actor=job.actor)
        except ValidationError:
            pass  # Another operation won the document lock; dispatcher backfills PDFs.
    return bool(changed)


def recover_expired_jobs():
    count = 0
    ids = list(FiscalJob.objects.filter(status='running', lease_until__lt=timezone.now()).values_list('pk', flat=True)[:100])
    for job_id in ids:
        with transaction.atomic():
            job = FiscalJob.objects.select_for_update().get(pk=job_id)
            if job.status != 'running' or not job.lease_until or job.lease_until >= timezone.now():
                continue
            if job.document_id:
                document = FiscalDocument.objects.select_for_update().get(pk=job.document_id)
                if job.operation == 'stamp':
                    # Even a death between PAC success and DB commit must never cause another stamp.
                    job.operation = 'recover'
                    if document.status == 'submitting':
                        document.status = 'uncertain'
                        document.save(update_fields=['status'])
                    if not document.request_xml:
                        # No signed XML means the remote operation could not have started.
                        job.operation = 'stamp'
                elif job.operation == 'cancel':
                    job.operation = 'cancellation_status'
                    job.payload = {}
            job.status = 'queued' if job.attempts < MAX_ATTEMPTS else 'review'
            job.claim_token = None
            job.lease_until = None
            job.available_at = timezone.now()
            job.finished_at = timezone.now() if job.status == 'review' else None
            job.message = 'La operación se interrumpió; se consultará el resultado antes de continuar.' if job.status == 'queued' else 'La operación requiere revisión después de varias interrupciones.'
            job.save()
            audit(job.actor, 'fiscal.job.interrupted', job.pk, {'next_operation': job.operation, 'status': job.status})
            count += 1
    return count


def dispatch_jobs():
    recover_expired_jobs()
    count = 0
    for job_id in FiscalJob.objects.filter(status='queued', available_at__lte=timezone.now()).order_by('created_at').values_list('pk', flat=True)[:100]:
        count += bool(_publish(job_id))
    # Documents issued before this feature or by demo management commands also get a PDF.
    for document in FiscalDocument.objects.exclude(xml='').filter(pdf_source_sha256='').exclude(jobs__status__in=ACTIVE).exclude(jobs__operation='pdf', jobs__status__in=['failed', 'review']).defer('pdf_content')[:20]:
        try:
            queue_job('pdf', document=document)
        except ValidationError:
            pass
    return count
