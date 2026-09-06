"""Owned customer access to proposed terms, receipt and separate acceptance."""
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from compliance.models import RegulatoryNotice
from compliance.workflows import accept_regulatory_notice, acknowledge_notice_receipt
from .models import Customer


class NoticeResponseForm(forms.Form):
    received = forms.BooleanField(label="Confirmo que recibí y pude consultar este aviso")
    accepted = forms.BooleanField(label="Acepto expresamente las condiciones descritas (opcional)", required=False, help_text="Puedes confirmar recepción sin aceptar el cambio. Conservas la opción de cancelar tu servicio.")


@login_required
def index(request):
    customer = get_object_or_404(Customer, user=request.user, is_active=True)
    notices = RegulatoryNotice.objects.filter(subscription__customer=customer, published_at__isnull=False).select_related("subscription__plan", "document").order_by("-published_at")
    return render(request, "core/portal_notices.html", {"notices": notices, "customer": customer})


@login_required
def respond(request, pk):
    customer = get_object_or_404(Customer, user=request.user, is_active=True)
    notice = get_object_or_404(RegulatoryNotice.objects.select_related("subscription__customer", "document"), pk=pk, subscription__customer=customer, published_at__isnull=False)
    form = NoticeResponseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            acknowledge_notice_receipt(notice.pk, request.user)
            if form.cleaned_data["accepted"]:
                accept_regulatory_notice(notice.pk, request.user, f"Casilla de aceptación expresa marcada en sesión del titular; usuario {request.user.pk}.")
        messages.success(request, "Tu respuesta quedó registrada. La recepción y la aceptación se conservan por separado.")
        return redirect("core:portal_notices")
    return render(request, "core/portal_notices.html", {"notices": [notice], "customer": customer, "form": form, "selected_notice": notice})
