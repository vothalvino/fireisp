from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from core.security import staff_required
from core.services import audit
from . import forms, models, services


@staff_required
def index(request):
    return render(request, "operations/index.html", {"tickets": models.Ticket.objects.select_related("customer")[:15], "work_orders": models.WorkOrder.objects.select_related("subscription__customer")[:15], "outages": models.Outage.objects.order_by("-started_at")[:10], "sites": models.Site.objects.all(), "equipment": models.Equipment.objects.select_related("sector")[:30], "sectors": models.Sector.objects.select_related("site")})


FORMS = {"ticket": (forms.TicketForm, models.Ticket, "Nuevo ticket"), "work-order": (forms.WorkOrderForm, models.WorkOrder, "Orden de trabajo"), "site": (forms.SiteForm, models.Site, "Sitio de red"), "sector": (forms.SectorForm, models.Sector, "Sector inalámbrico"), "equipment": (forms.EquipmentForm, models.Equipment, "Equipo de red"), "outage": (forms.OutageForm, models.Outage, "Falla de red")}


@staff_required
def edit(request, kind, pk=None):
    form_class, model, title = FORMS[kind]
    instance = get_object_or_404(model, pk=pk) if pk else None
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        record = form.save()
        audit(request.user, f"operations.{kind}.saved", record)
        messages.success(request, "Registro guardado.")
        if kind == "ticket":
            return redirect("operations:ticket_detail", pk=record.pk)
        if kind == "work-order":
            return redirect("operations:work_detail", pk=record.pk)
        if kind == "outage":
            return redirect("operations:outage_detail", pk=record.pk)
        return redirect("operations:index")
    return render(request, "form.html", {"title": title, "form": form})


@staff_required
def ticket_detail(request, pk):
    ticket = get_object_or_404(models.Ticket.objects.select_related("customer", "subscription"), pk=pk)
    form = forms.ResolveTicketForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            services.resolve_ticket(ticket.pk, request.user, form.cleaned_data["resolution"])
            messages.success(request, "Ticket resuelto. La bitácora conserva el resultado.")
            return redirect("operations:ticket_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "operations/ticket.html", {"ticket": ticket, "form": form})


@staff_required
def work_detail(request, pk):
    work = get_object_or_404(models.WorkOrder.objects.select_related("subscription__customer"), pk=pk)
    form = forms.CompleteWorkForm(request.POST or None, initial={"evidence": work.completion_evidence})
    if request.method == "POST" and form.is_valid():
        try:
            services.complete_work_order(work.pk, request.user, form.cleaned_data["evidence"])
            messages.success(request, "Orden completada. Se registró la activación real cuando corresponde.")
            return redirect("operations:work_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "operations/work.html", {"work": work, "form": form})


@staff_required
def outage_detail(request, pk):
    outage = get_object_or_404(models.Outage, pk=pk)
    form = forms.CreditPeriodForm(request.POST or None, outage=outage)
    if request.method == "POST" and form.is_valid():
        try:
            services.record_outage_credit(outage, actor=request.user, **form.cleaned_data)
            messages.success(request, "Compensación calculada. Revisa su aplicación en el saldo.")
            return redirect("operations:outage_detail", pk=pk)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(request, "operations/outage.html", {"outage": outage, "form": form, "credits": outage.credits.select_related("subscription__customer")})
