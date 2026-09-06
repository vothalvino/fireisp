from django import forms
from django.core.exceptions import ValidationError
from .models import Ticket, WorkOrder, Site, Sector, Equipment, Outage


class OperationsModelForm(forms.ModelForm):
    LABELS = {
        "customer": "Cliente", "subscription": "Servicio", "kind": "Tipo", "subject": "Asunto",
        "description": "Descripción", "channel": "Canal de atención", "assigned_to": "Responsable",
        "scheduled_at": "Fecha programada", "notes": "Notas", "organization": "Operador", "name": "Nombre",
        "address": "Domicilio", "latitude": "Latitud", "longitude": "Longitud", "structure_height_m": "Altura de estructura (m)",
        "owner_permission": "Permiso del propietario", "permit_status": "Estado de permisos", "permit_evidence": "Expediente de permisos",
        "permit_expires_on": "Vigencia del permiso", "site": "Sitio", "frequency_mhz": "Frecuencia (MHz)",
        "channel_width_mhz": "Ancho de canal (MHz)", "outdoor": "Uso en exteriores", "dfs_required": "DFS requerido",
        "dfs_enabled": "DFS habilitado", "tpc_required": "TPC requerido", "tpc_enabled": "TPC habilitado",
        "regulatory_basis": "Disposición de radio aplicable", "capacity_mbps": "Capacidad (Mbps)", "serial_number": "Número de serie",
        "model": "Modelo exacto", "role": "Función", "sector": "Sector", "status": "Estado", "homologation_certificate": "Certificado de homologación",
        "homologation_verified": "Homologación verificada", "homologation_evidence": "Evidencia de homologación", "firmware": "Versión de firmware",
        "regulatory_profile": "Perfil de país", "tx_power_dbm": "Potencia de transmisión (dBm)", "antenna_gain_dbi": "Ganancia de antena (dBi)",
        "cable_loss_db": "Pérdida de cable (dB)", "allowed_eirp_dbm": "Límite de PIRE documentado (dBm)", "title": "Nombre de la falla",
        "started_at": "Inicio", "ended_at": "Recuperación", "provider_attributable": "Falla atribuible al operador",
        "attribution_evidence": "Fundamento de atribución", "subscriptions": "Servicios afectados",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.label = self.LABELS.get(name, field.label)


class TicketForm(OperationsModelForm):
    class Meta:
        model = Ticket
        fields = ["customer", "subscription", "kind", "subject", "description", "channel", "assigned_to"]


class ResolveTicketForm(forms.Form):
    resolution = forms.CharField(label="Resolución", widget=forms.Textarea)


class WorkOrderForm(OperationsModelForm):
    class Meta:
        model = WorkOrder
        fields = ["subscription", "kind", "scheduled_at", "assigned_to", "notes"]
        widgets = {"scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}


class CompleteWorkForm(forms.Form):
    evidence = forms.CharField(label="Pruebas realizadas y aceptación", widget=forms.Textarea)


class SiteForm(OperationsModelForm):
    class Meta:
        model = Site
        fields = "__all__"
        widgets = {"permit_expires_on": forms.DateInput(attrs={"type": "date"})}


class SectorForm(OperationsModelForm):
    class Meta:
        model = Sector
        fields = "__all__"


class EquipmentForm(OperationsModelForm):
    class Meta:
        model = Equipment
        fields = "__all__"


class OutageForm(OperationsModelForm):
    class Meta:
        model = Outage
        fields = "__all__"
        widgets = {"started_at": forms.DateTimeInput(attrs={"type": "datetime-local"}), "ended_at": forms.DateTimeInput(attrs={"type": "datetime-local"})}

    def clean(self):
        cleaned = super().clean()
        organization = cleaned.get("organization")
        for subscription in cleaned.get("subscriptions", []):
            if organization and subscription.customer.organization_id != organization.pk:
                raise ValidationError("Todos los afectados deben pertenecer al operador de la falla.")
        return cleaned


class CreditPeriodForm(forms.Form):
    subscription = forms.ModelChoiceField(queryset=None, label="Servicio afectado")
    period_start = forms.DateTimeField(label="Inicio del periodo cobrado", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    period_end = forms.DateTimeField(label="Fin del periodo cobrado", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}))
    price = forms.DecimalField(label="Precio del periodo con impuestos (MXN)", max_digits=12, decimal_places=2, min_value=0)

    def __init__(self, *args, outage, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["subscription"].queryset = outage.subscriptions.all()
