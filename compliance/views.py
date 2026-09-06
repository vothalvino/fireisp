import csv
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from core.models import Organization
from core.security import staff_required
from core.services import audit
from . import forms, models, services
from . import workflows


@staff_required
def index(request):
    organizations = [(org, services.legal_readiness(org)) for org in Organization.objects.all()]
    return render(request, "compliance/index.html", {"organizations": organizations, "requirements": models.LegalRequirement.objects.select_related("organization"), "documents": models.DocumentVersion.objects.all(), "arco_requests": models.ARCORequest.objects.select_related("customer").order_by("response_due_on"), "holds": models.RetentionHold.objects.filter(released_at__isnull=True).select_related("customer"), "policies": models.RetentionPolicy.objects.all(), "consents": models.Consent.objects.select_related("customer", "document").order_by("-accepted_at")[:20], "holidays": models.BusinessHoliday.objects.order_by("date")[:30], "cancellations": models.CancellationRequest.objects.select_related("subscription__customer").order_by("-requested_at")[:20], "regulatory_notices": models.RegulatoryNotice.objects.select_related("subscription__customer").order_by("-created_at")[:20], "plan_registrations": models.PlanRegistration.objects.select_related("plan"), "disposals": models.RetentionDisposal.objects.select_related("customer").order_by("-created_at")[:20]})


FORMS = {"requirement": (forms.LegalRequirementForm, models.LegalRequirement, "Obligación y evidencia"), "document": (forms.DocumentForm, models.DocumentVersion, "Versión de documento"), "policy": (forms.RetentionPolicyForm, models.RetentionPolicy, "Política de conservación"), "hold": (forms.RetentionHoldForm, models.RetentionHold, "Orden de conservación"), "holiday": (forms.HolidayForm, models.BusinessHoliday, "Calendario de días hábiles"), "plan-registration": (forms.PlanRegistrationForm, models.PlanRegistration, "Registro de la tarifa del plan")}


@staff_required
def edit(request, kind, pk=None):
    form_class, model, title = FORMS[kind]
    instance = get_object_or_404(model, pk=pk) if pk else None
    kwargs = {"instance": instance}
    if kind in {"requirement", "document", "plan-registration"}:
        kwargs["actor"] = request.user
    form = form_class(request.POST or None, **kwargs)
    if request.method == "POST" and form.is_valid():
        record = form.save()
        audit(request.user, f"compliance.{kind}.saved", record)
        messages.success(request, "Registro guardado con evidencia de la acción.")
        return redirect("compliance:index")
    return render(request, "form.html", {"title": title, "form": form})


@staff_required
def register_export(request):
    def safe(value):
        value = str(value or "")
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")) else value
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="fireisp-obligaciones.csv"'
    response.write("\ufeff")
    writer = csv.writer(response)
    writer.writerow(["Operador", "Código", "Obligación", "Fundamento", "Fuente", "Vigencia", "Aplica", "Estado", "Vencimiento", "Evidencia"])
    for record in models.LegalRequirement.objects.select_related("organization"):
        writer.writerow([safe(value) for value in [record.organization.name, record.code, record.title, record.legal_reference, record.source_url, record.effective_on, record.applicable, record.get_status_display(), record.due_on, record.evidence]])
    audit(request.user, "compliance.register_exported", "legal_register")
    return response


@staff_required
def document_detail(request, pk):
    document = get_object_or_404(models.DocumentVersion, pk=pk)
    return render(request, "compliance/document.html", {"document": document})


@staff_required
def arco_create(request):
    form = forms.ARCOCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        record = services.create_arco_request(actor=request.user, **form.cleaned_data)
        return redirect("compliance:arco_detail", pk=record.pk)
    return render(request, "form.html", {"title": "Nueva solicitud ARCO", "form": form})


@staff_required
def arco_detail(request, pk):
    record = get_object_or_404(models.ARCORequest.objects.select_related("customer"), pk=pk)
    form = forms.ARCOActionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        action, evidence = form.cleaned_data["action"], form.cleaned_data["evidence"]
        try:
            if action == "identity":
                services.verify_arco_identity(pk, request.user, evidence)
            elif action in {"grant", "deny"}:
                services.respond_arco(pk, request.user, evidence, action == "grant")
            elif action.startswith("extend_"):
                services.extend_arco(pk, request.user, action.removeprefix("extend_"), form.cleaned_data["reason"], evidence)
            else:
                services.complete_arco(pk, request.user, evidence)
            messages.success(request, "Acción ARCO registrada.")
            return redirect("compliance:arco_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "compliance/arco.html", {"record": record, "form": form})


@staff_required
def consent_create(request):
    form = forms.ConsentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.record_consent(actor=request.user, **form.cleaned_data)
            messages.success(request, "Constancia guardada con copia y hash de la versión entregada.")
            return redirect("compliance:index")
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "form.html", {"title": "Registrar entrega o consentimiento", "form": form})


@staff_required
@require_POST
def consent_withdraw(request, pk):
    try:
        services.withdraw_consent(pk, request.user)
        messages.success(request, "Consentimiento revocado.")
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
    return redirect("compliance:index")


@staff_required
def hold_release(request, pk):
    get_object_or_404(models.RetentionHold, pk=pk)
    form = forms.ReleaseHoldForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        services.release_retention_hold(pk, request.user, form.cleaned_data["reason"])
        messages.success(request, "Conservación liberada. No se eliminaron datos.")
        return redirect("compliance:index")
    return render(request, "form.html", {"title": "Liberar orden de conservación", "form": form})


@staff_required
def notice_create(request):
    form = forms.RegulatoryNoticeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        notice = form.save(commit=False)
        notice.created_by = request.user
        notice.save()
        audit(request.user, "regulatory_notice.drafted", notice.pk)
        return redirect("compliance:notice_detail", pk=notice.pk)
    return render(request, "form.html", {"title": "Preparar aviso regulatorio", "form": form})


@staff_required
def notice_detail(request, pk):
    notice = get_object_or_404(models.RegulatoryNotice.objects.select_related("subscription__customer", "document"), pk=pk)
    form = forms.NoticeActionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            if form.cleaned_data["action"] == "publish":
                workflows.publish_regulatory_notice(pk, request.user)
            elif form.cleaned_data["action"] == "delivered":
                workflows.confirm_notice_delivery(pk, request.user, form.cleaned_data["channel"], form.cleaned_data["evidence"])
            else:
                workflows.accept_regulatory_notice(pk, request.user, form.cleaned_data["evidence"])
            return redirect("compliance:notice_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "compliance/notice.html", {"notice": notice, "form": form, "ready": workflows.regulatory_notice_ready(notice)})


@staff_required
def disposal_create(request):
    form = forms.DisposalPreviewForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            preview = workflows.preview_retention_disposal(actor=request.user, **form.cleaned_data)
            return redirect("compliance:disposal_detail", pk=preview.pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "form.html", {"title": "Revisar contenido con plazo de conservación vencido", "form": form})


@staff_required
def disposal_detail(request, pk):
    preview = get_object_or_404(models.RetentionDisposal.objects.select_related("customer"), pk=pk)
    form = forms.DisposalConfirmForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            workflows.execute_retention_disposal(pk, request.user, **form.cleaned_data)
            messages.success(request, "Contenido indicado suprimido; la bitácora conserva el alcance de la operación.")
            return redirect("compliance:disposal_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "compliance/disposal.html", {"preview": preview, "form": form})


@staff_required
def cancellation_create(request):
    form = forms.StaffCancellationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        record = services.cancel_subscription(form.cleaned_data["subscription"].pk, request.user, form.cleaned_data["channel"], form.cleaned_data["reason"])
        messages.success(request, f"Cancelación efectiva registrada. Folio {record.folio}; conserva la atención por el canal recibido.")
        return redirect("compliance:index")
    return render(request, "form.html", {"title": "Registrar cancelación recibida por otro canal", "form": form, "submit_label": "Cancelar servicio y emitir folio"})
