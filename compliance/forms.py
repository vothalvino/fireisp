from django import forms
from django.utils import timezone
from core.models import Customer, Subscription
from .models import LegalRequirement, DocumentVersion, RetentionPolicy, RetentionHold, BusinessHoliday, ARCORequest, Consent, RegulatoryNotice, RetentionDisposal, PlanRegistration


class ComplianceModelForm(forms.ModelForm):
    LABELS = {
        "organization": "Operador", "code": "Código de obligación", "title": "Título", "source_url": "Fuente oficial",
        "legal_reference": "Artículo o disposición", "effective_on": "Vigente desde", "applicable": "Aplica al operador",
        "production_required": "Requisito previo a producción", "status": "Estado", "evidence": "Evidencia de revisión",
        "due_on": "Fecha límite", "expires_on": "Vigencia de evidencia", "notes": "Notas", "kind": "Tipo de documento",
        "version": "Versión", "content": "Contenido completo", "registration_reference": "Referencia de registro PROFECO/RPC",
        "category": "Categoría de datos", "lawful_basis": "Fundamento y finalidad", "retention_days": "Plazo de conservación (días)",
        "disposal_method": "Método de eliminación", "approved": "Política revisada", "reviewed_on": "Fecha de revisión",
        "customer": "Cliente", "reason": "Fundamento de conservación", "authority_reference": "Referencia de autoridad o expediente",
        "date": "Fecha", "name": "Motivo", "is_working_day": "Día habilitado como hábil", "source_reference": "Disposición o fuente del calendario",
        "subscription": "Servicio", "body": "Texto íntegro del aviso", "requires_consent": "Exige aceptación expresa",
        "document": "Versión propuesta del documento", "renewal_amount_mxn": "Importe de renovación con impuestos (MXN)",
        "renewal_frequency": "Periodicidad del cargo",
        "plan": "Versión de plan", "tariff_reference": "Folio de registro de tarifa", "registered_on": "Fecha de registro",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.label = self.LABELS.get(name, field.label)


class LegalRequirementForm(ComplianceModelForm):
    class Meta:
        model = LegalRequirement
        exclude = ["reviewed_by", "reviewed_at"]
        widgets = {field: forms.DateInput(attrs={"type": "date"}) for field in ["effective_on", "due_on", "expires_on"]}

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("status") == "approved":
            self.instance.reviewed_by = self.actor
            self.instance.reviewed_at = timezone.now()
        else:
            self.instance.reviewed_by = None
            self.instance.reviewed_at = None
        return cleaned


class DocumentForm(ComplianceModelForm):
    class Meta:
        model = DocumentVersion
        exclude = ["approved_by"]
        widgets = {"effective_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor

    def clean(self):
        cleaned = super().clean()
        self.instance.approved_by = self.actor if cleaned.get("status") == "approved" else None
        return cleaned


class ARCOCreateForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=Customer.objects.all(), label="Cliente")
    request_type = forms.ChoiceField(choices=ARCORequest._meta.get_field("request_type").choices, label="Derecho solicitado")
    description = forms.CharField(label="Solicitud", widget=forms.Textarea)


class ARCOActionForm(forms.Form):
    action = forms.ChoiceField(label="Acción", choices=[("identity", "Verificar identidad"), ("grant", "Comunicar respuesta favorable"), ("deny", "Comunicar respuesta fundada negativa"), ("extend_response", "Ampliar plazo de respuesta"), ("extend_implementation", "Ampliar plazo de ejecución"), ("complete", "Registrar ejecución")])
    evidence = forms.CharField(label="Evidencia o respuesta", widget=forms.Textarea)
    reason = forms.CharField(label="Justificación de ampliación", required=False, widget=forms.Textarea)


class RetentionPolicyForm(ComplianceModelForm):
    class Meta:
        model = RetentionPolicy
        fields = "__all__"
        widgets = {"reviewed_on": forms.DateInput(attrs={"type": "date"})}


class RetentionHoldForm(ComplianceModelForm):
    class Meta:
        model = RetentionHold
        fields = ["customer", "category", "reason", "authority_reference"]


class HolidayForm(ComplianceModelForm):
    class Meta:
        model = BusinessHoliday
        fields = "__all__"
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}


class ConsentForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=Customer.objects.all(), label="Cliente")
    document = forms.ModelChoiceField(queryset=DocumentVersion.objects.filter(status="approved"), label="Versión entregada")
    purpose = forms.ChoiceField(choices=Consent._meta.get_field("purpose").choices, label="Finalidad")
    channel = forms.CharField(max_length=30, label="Canal")
    evidence = forms.CharField(label="Constancia de entrega o manifestación expresa", widget=forms.Textarea)


class ReleaseHoldForm(forms.Form):
    reason = forms.CharField(label="Fundamento de liberación", widget=forms.Textarea)


class RegulatoryNoticeForm(ComplianceModelForm):
    class Meta:
        model = RegulatoryNotice
        fields = ["subscription", "kind", "effective_on", "title", "body", "requires_consent", "renewal_amount_mxn", "renewal_frequency", "document"]
        widgets = {"effective_on": forms.DateInput(attrs={"type": "date"})}


class NoticeActionForm(forms.Form):
    action = forms.ChoiceField(label="Acción", choices=[("publish", "Publicar en el portal"), ("delivered", "Registrar evidencia de entrega"), ("accept", "Registrar aceptación expresa del cliente")])
    channel = forms.CharField(label="Canal de entrega", max_length=40, required=False)
    evidence = forms.CharField(label="Evidencia de entrega o aceptación", required=False, widget=forms.Textarea)


class DisposalPreviewForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=Customer.objects.all(), label="Cliente")
    category = forms.ChoiceField(label="Contenido a revisar", choices=RetentionDisposal.CATEGORY)


class DisposalConfirmForm(forms.Form):
    confirmed = forms.BooleanField(label="Revisé los registros indicados y confirmo la supresión irreversible de su contenido")
    external_copies_evidence = forms.CharField(label="Tratamiento de copias externas, archivos y respaldos", widget=forms.Textarea, help_text="Documenta las medidas y sus plazos. Esta operación sólo suprime el contenido indicado en la aplicación.")


class PlanRegistrationForm(ComplianceModelForm):
    class Meta:
        model = PlanRegistration
        exclude = ["reviewed_by", "reviewed_at", "plan_snapshot"]
        widgets = {field: forms.DateInput(attrs={"type": "date"}) for field in ["registered_on", "effective_on", "expires_on"]}

    def __init__(self, *args, actor=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.actor = actor
        self.fields["approved"].label = "Registro de tarifa revisado"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("approved") and cleaned.get("plan"):
            self.instance.reviewed_by = self.actor
            self.instance.reviewed_at = timezone.now()
            self.instance.plan_snapshot = PlanRegistration.snapshot(cleaned["plan"])
        return cleaned


class StaffCancellationForm(forms.Form):
    subscription = forms.ModelChoiceField(queryset=Subscription.objects.select_related("customer", "plan"), label="Servicio a cancelar")
    channel = forms.CharField(label="Canal en que se recibió la cancelación", max_length=30, help_text="Por ejemplo: teléfono, correo o presencial. No obligues al cliente a usar un canal distinto.")
    reason = forms.CharField(label="Motivo (opcional)", required=False, widget=forms.Textarea)
    confirmed = forms.BooleanField(label="Confirmé la identidad o representación y registro la cancelación solicitada")
