from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.http import HttpResponse,Http404
from django.shortcuts import get_object_or_404,redirect,render
from django.views.decorators.http import require_POST
from core.models import Organization
from core.security import staff_required
from core.secrets import encrypt
from core.services import audit
from billing.models import Allocation,Invoice,CreditMemo
from .models import FiscalDocument,FiscalProfile
from .forms import CancellationForm,IssueForm,ProfileForm,GlobalForm
from . import services
from .jobs import queue_job,pdf_ready


@staff_required
def index(request):
    return render(request,'fiscal/index.html',{'documents':FiscalDocument.objects.select_related('invoice__customer').defer('pdf_content')[:100]})


def _document_redirect(doc):
    return redirect('fiscal:global_detail',pk=doc.pk) if doc.kind=='global' else redirect('fiscal:invoice',pk=doc.invoice_id)


@staff_required
def global_create(request):
    form=GlobalForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                document=services.prepare_global(Organization.objects.first(),actor=request.user,defer_build=True,**form.cleaned_data)
                queue_job('stamp',document=document,actor=request.user)
            messages.success(request,'Solicitud de factura global registrada. El resultado aparecerá en este documento.')
            return redirect('fiscal:global_detail',pk=document.pk)
        except (ValidationError,FiscalProfile.DoesNotExist) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else 'Configura primero Finkok DEMO.')
    return render(request,'form.html',{'form':form,'title':'Factura global de público en general · DEMO'})


@staff_required
def global_detail(request,pk):
    document=get_object_or_404(FiscalDocument,pk=pk,kind='global')
    return render(request,'fiscal/global.html',{'doc':document,'items':document.global_batch.items.select_related('invoice__customer')})


@require_POST
@staff_required
def credit_issue(request,pk):
    memo=get_object_or_404(CreditMemo,pk=pk)
    try:
        with transaction.atomic():
            document=services.prepare_credit_document(memo,request.user,defer_build=True)
            queue_job('stamp',document=document,actor=request.user)
        messages.success(request,'Solicitud de CFDI de egreso registrada.')
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
    return redirect('fiscal:invoice',pk=memo.invoice_id)


@staff_required
def settings_view(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    profile,_=FiscalProfile.objects.get_or_create(organization=Organization.objects.first())
    form=ProfileForm(request.POST or None,request.FILES or None,instance=profile)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                profile=form.save(commit=False)
                for field in ['username','password']:
                    if form.cleaned_data[field]:
                        setattr(profile,field+'_encrypted',encrypt(form.cleaned_data[field]))
                profile.verified_at=None
                profile.save()
                for field,fiel in [('csd_zip',False),('fiel_zip',True)]:
                    if form.cleaned_data[field]:
                        services.import_certificate_zip(profile,form.cleaned_data[field].read(1_000_001),form.cleaned_data['certificate_password'],fiel)
                audit(request.user,'fiscal.profile.updated',profile.pk,{'environment':'demo'})
            messages.success(request,'Configuración DEMO guardada. Verifica la conexión antes de timbrar.')
            return redirect('fiscal:settings')
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'fiscal/settings.html',{'form':form,'profile':profile})


@require_POST
@staff_required
def verify(request):
    if not request.user.is_superuser:
        raise PermissionDenied
    profile=get_object_or_404(FiscalProfile,organization=Organization.objects.first())
    try:
        queue_job('verify',profile=profile,actor=request.user)
        messages.success(request,'Verificación solicitada. Actualiza esta página para consultar el resultado.')
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
    return redirect('fiscal:settings')


@staff_required
def invoice(request,pk):
    invoice=get_object_or_404(Invoice.objects.select_related('customer'),pk=pk)
    form=IssueForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            with transaction.atomic():
                doc=services.prepare_document(invoice,request.user,defer_build=True,**form.cleaned_data)
                queue_job('stamp',document=doc,actor=request.user)
            messages.success(request,'Solicitud de CFDI registrada. El resultado aparecerá en esta página.')
            return redirect('fiscal:invoice',pk=pk)
        except (ValidationError,FiscalProfile.DoesNotExist) as exc:
            form.add_error(None,exc if isinstance(exc,ValidationError) else 'Configura Finkok DEMO antes de facturar.')
    return render(request,'fiscal/invoice.html',{'invoice':invoice,'form':form,'documents':invoice.fiscal_documents.defer('pdf_content'),
        'allocations':invoice.allocations.filter(payment__reversal__isnull=True).select_related('payment')})


@require_POST
@staff_required
def recover(request,pk):
    doc=get_object_or_404(FiscalDocument,pk=pk)
    try:
        queue_job('recover',document=doc,actor=request.user)
        messages.success(request,'Recuperación solicitada. Se consultará el mismo XML al PAC.')
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
    return _document_redirect(doc)


@require_POST
@staff_required
def complement(request,pk):
    allocation=get_object_or_404(Allocation,pk=pk)
    try:
        with transaction.atomic():
            doc=services.prepare_document(allocation.invoice,request.user,allocation=allocation,defer_build=True)
            queue_job('stamp',document=doc,actor=request.user)
        messages.success(request,'Solicitud de complemento de pago 2.0 registrada.')
    except (ValidationError,FiscalDocument.DoesNotExist,FiscalProfile.DoesNotExist) as exc:
        messages.error(request,'; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Se requiere la factura PPD timbrada y la configuración del PAC.')
    return redirect('fiscal:invoice',pk=allocation.invoice_id)


@staff_required
def cancel(request,pk):
    doc=get_object_or_404(FiscalDocument,pk=pk)
    form=CancellationForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            queue_job('cancel',document=doc,actor=request.user,payload={'reason':form.cleaned_data['reason'],'replacement':str(form.cleaned_data['replacement'] or '')})
            messages.success(request,'Solicitud de cancelación registrada. Consulta el estado hasta obtener confirmación del SAT DEMO.')
            return _document_redirect(doc)
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'form.html',{'form':form,'title':f'Cancelar CFDI DEMO {doc.uuid}'})


@require_POST
@staff_required
def cancellation_status(request,pk):
    doc=get_object_or_404(FiscalDocument,pk=pk)
    try:
        queue_job('cancellation_status',document=doc,actor=request.user)
        messages.info(request,'Consulta de cancelación solicitada. Actualiza esta página para ver el resultado.')
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
    return _document_redirect(doc)


@login_required
def download(request,pk,format):
    doc=get_object_or_404(FiscalDocument.objects.select_related('invoice__customer'),pk=pk)
    if not request.user.is_staff and (not doc.invoice_id or doc.invoice.customer.user_id!=request.user.pk):
        raise PermissionDenied
    if not doc.xml:
        raise Http404('El documento todavía no está timbrado.')
    if format=='xml':
        response=HttpResponse(doc.xml,content_type='application/xml')
    elif format=='pdf':
        if not pdf_ready(doc):
            try:
                queue_job('pdf',document=doc,actor=request.user)
            except ValidationError:
                pass  # An existing fiscal operation finishes before PDF preparation.
            response=render(request,'fiscal/pdf_pending.html',{'doc':doc},status=202)
            response['Retry-After']='10'
        else:
            response=HttpResponse(bytes(doc.pdf_content),content_type='application/pdf')
    elif format=='acuse' and doc.cancellation_xml:
        response=HttpResponse(doc.cancellation_xml,content_type='application/xml')
    else:
        raise PermissionDenied
    suffix='xml' if format=='acuse' else format
    if response.status_code==200:
        response['Content-Disposition']=f'attachment; filename="DEMO-{doc.uuid}-{format}.{suffix}"'
    response['Cache-Control']='private, no-store'
    response['X-Content-Type-Options']='nosniff'
    return response
