import hashlib
import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


def cancellation_folio():
    return f"C-{uuid.uuid4().hex[:16].upper()}"


class LegalRequirement(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT, related_name="legal_requirements")
    code = models.SlugField(max_length=100)
    title = models.CharField(max_length=200)
    source_url = models.URLField(max_length=700)
    legal_reference = models.CharField(max_length=200)
    effective_on = models.DateField(null=True, blank=True)
    applicable = models.BooleanField(default=True)
    production_required = models.BooleanField(default=True)
    status = models.CharField(max_length=20, choices=[("unreviewed", "Sin verificar"), ("in_progress", "En trámite"), ("approved", "Verificado")], default="unreviewed")
    evidence = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    due_on = models.DateField(null=True, blank=True)
    expires_on = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["code"]
        constraints = [models.UniqueConstraint(fields=["organization", "code"], name="unique_organization_legal_requirement")]

    def clean(self):
        if (self.status == "approved" or not self.applicable) and not self.evidence.strip():
            raise ValidationError("Documenta la verificación o el fundamento de no aplicabilidad.")
        if self.status == "approved" and not (self.reviewed_by_id and self.reviewed_at):
            raise ValidationError("La verificación necesita responsable y fecha.")

    def __str__(self):
        return self.title


class DocumentVersion(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=[("contract", "Contrato de adhesión"), ("privacy", "Aviso de privacidad"), ("rights", "Carta de derechos"), ("network", "Política de gestión de red")])
    version = models.CharField(max_length=40)
    title = models.CharField(max_length=200)
    content = models.TextField()
    effective_on = models.DateField(default=timezone.localdate)
    status = models.CharField(max_length=20, choices=[("draft", "Borrador"), ("approved", "Aprobado para uso")], default="draft")
    registration_reference = models.CharField(max_length=200, blank=True, help_text="Registro PROFECO/RPC cuando corresponda.")
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "-effective_on", "-created_at"]
        constraints = [models.UniqueConstraint(fields=["organization", "kind", "version"], name="unique_legal_document_version")]

    @property
    def content_hash(self):
        return hashlib.sha256(self.content.encode()).hexdigest()

    def clean(self):
        if self.status == "approved" and not self.approved_by_id:
            raise ValidationError("Identifica a la persona que aprobó esta versión.")
        if self.status == "approved" and self.kind == "contract" and not self.registration_reference.strip():
            raise ValidationError("Registra la referencia PROFECO/RPC del contrato.")
        if self.pk and (Consent.objects.filter(document_id=self.pk).exists() or RegulatoryNotice.objects.filter(document_id=self.pk, published_at__isnull=False).exists()):
            original = type(self).objects.get(pk=self.pk)
            for field in ["organization_id", "kind", "version", "title", "content", "effective_on", "registration_reference"]:
                if getattr(original, field) != getattr(self, field):
                    raise ValidationError("La versión ya tiene constancias. Crea una nueva versión para modificarla.")

    def __str__(self):
        return f"{self.get_kind_display()} · {self.version}"

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Consent(models.Model):
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="legal_consents")
    document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, related_name="consents")
    purpose = models.CharField(max_length=30, choices=[("contract", "Aceptación de contrato"), ("privacy_notice", "Entrega de aviso"), ("marketing", "Consentimiento publicitario"), ("autopay", "Autorización de cargo recurrente"), ("document_delivery", "Entrega de documento")])
    channel = models.CharField(max_length=30)
    evidence = models.TextField()
    content_snapshot = models.TextField(editable=False)
    content_hash = models.CharField(max_length=64, editable=False)
    accepted_at = models.DateTimeField(default=timezone.now)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    def clean(self):
        if self.document_id and self.customer_id and self.document.organization_id != self.customer.organization_id:
            raise ValidationError("El documento pertenece a otro operador.")
        if not self.evidence.strip():
            raise ValidationError("Se requiere evidencia de la manifestación o entrega.")
        if self.content_hash != hashlib.sha256(self.content_snapshot.encode()).hexdigest():
            raise ValidationError("La copia y su huella de integridad no coinciden.")
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            for field in ["customer_id", "document_id", "purpose", "channel", "evidence", "content_snapshot", "content_hash", "accepted_at", "recorded_by_id"]:
                if getattr(original, field) != getattr(self, field):
                    raise ValidationError("La constancia original es inmutable. Registra una nueva constancia.")
            if original.withdrawn_at and self.withdrawn_at != original.withdrawn_at:
                raise ValidationError("La revocación registrada es inmutable.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class BusinessHoliday(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.CASCADE)
    date = models.DateField()
    name = models.CharField(max_length=200)
    is_working_day = models.BooleanField(default=False, help_text="Marca si una disposición habilita esta fecha como hábil.")
    source_reference = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["date"]
        constraints = [models.UniqueConstraint(fields=["organization", "date"], name="unique_organization_holiday")]

    def __str__(self):
        return f"{self.date} · {self.name}"


class ARCORequest(models.Model):
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="arco_requests")
    request_type = models.CharField(max_length=20, choices=[("access", "Acceso"), ("rectification", "Rectificación"), ("cancellation", "Cancelación de datos"), ("opposition", "Oposición")])
    description = models.TextField()
    received_on = models.DateField(default=timezone.localdate)
    identity_verified_at = models.DateTimeField(null=True, blank=True)
    identity_evidence = models.TextField(blank=True, help_text="Referencia a la verificación, no una copia de identificación.")
    status = models.CharField(max_length=25, choices=[("pending_identity", "Verificar identidad"), ("in_review", "En revisión"), ("decision_sent", "Respuesta comunicada"), ("completed", "Atendida")], default="pending_identity")
    response_due_on = models.DateField(null=True, blank=True, editable=False)
    decision = models.TextField(blank=True)
    decision_sent_on = models.DateField(null=True, blank=True)
    granted = models.BooleanField(null=True, blank=True)
    implementation_due_on = models.DateField(null=True, blank=True, editable=False)
    completed_on = models.DateField(null=True, blank=True)
    implementation_evidence = models.TextField(blank=True)

    def __str__(self):
        return f"ARCO-{self.pk} · {self.get_request_type_display()}"


class ARCOExtension(models.Model):
    request = models.ForeignKey(ARCORequest, on_delete=models.PROTECT, related_name="extensions")
    stage = models.CharField(max_length=20, choices=[("response", "Respuesta"), ("implementation", "Ejecución")])
    reason = models.TextField()
    notified_on = models.DateField()
    notification_evidence = models.TextField()
    previous_due_on = models.DateField()
    extended_due_on = models.DateField()
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["request", "stage"], name="one_arco_extension_per_stage")]


class RetentionPolicy(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    category = models.CharField(max_length=100)
    lawful_basis = models.TextField()
    retention_days = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    disposal_method = models.TextField()
    approved = models.BooleanField(default=False)
    reviewed_on = models.DateField(null=True, blank=True)
    source_url = models.URLField(max_length=700, blank=True)

    def clean(self):
        if self.approved and (not self.reviewed_on or not self.lawful_basis.strip() or not self.disposal_method.strip()):
            raise ValidationError("La política requiere fecha de revisión, fundamento y método de eliminación.")
        if self.reviewed_on and self.reviewed_on > timezone.localdate():
            raise ValidationError("La revisión no puede tener fecha futura.")

    def __str__(self):
        return self.category


class RetentionHold(models.Model):
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT)
    category = models.CharField(max_length=100, help_text="Usa * para todas las categorías del cliente.")
    reason = models.TextField()
    authority_reference = models.CharField(max_length=200, blank=True)
    placed_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.TextField(blank=True)


class CancellationRequest(models.Model):
    subscription = models.OneToOneField("core.Subscription", on_delete=models.PROTECT, related_name="cancellation")
    folio = models.CharField(max_length=25, unique=True, default=cancellation_folio, editable=False)
    channel = models.CharField(max_length=30)
    reason = models.TextField(blank=True)
    requested_at = models.DateTimeField(auto_now_add=True)
    effective_at = models.DateTimeField(default=timezone.now)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    network_disconnect_pending = models.BooleanField(default=True)

    def __str__(self):
        return self.folio


class RegulatoryNotice(models.Model):
    subscription = models.ForeignKey("core.Subscription", on_delete=models.PROTECT, related_name="regulatory_notices")
    kind = models.CharField(max_length=24, choices=[("contract_change", "Cambio contractual: 30 días"), ("automatic_renewal", "Renovación automática: 5 días")])
    effective_on = models.DateField()
    title = models.CharField(max_length=160)
    body = models.TextField()
    requires_consent = models.BooleanField(default=True)
    renewal_amount_mxn = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    renewal_frequency = models.CharField(max_length=60, blank=True)
    document = models.ForeignKey(DocumentVersion, on_delete=models.PROTECT, null=True, blank=True)
    notification = models.OneToOneField("core.Notification", on_delete=models.PROTECT, null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="regulatory_notices_created")
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    delivery_channel = models.CharField(max_length=40, blank=True)
    delivered_on = models.DateField(null=True, blank=True)
    delivery_evidence = models.TextField(blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    acceptance_evidence = models.TextField(blank=True)

    @property
    def minimum_notice_days(self):
        return 30 if self.kind == "contract_change" else 5

    @property
    def notify_by(self):
        from datetime import timedelta
        return self.effective_on - timedelta(days=self.minimum_notice_days)

    def clean(self):
        if self.document_id and self.document.organization_id != self.subscription.customer.organization_id:
            raise ValidationError("El documento pertenece a otro operador.")
        if self.kind == "contract_change" and not self.requires_consent:
            raise ValidationError("Este flujo de cambios requiere consentimiento expreso del cliente.")
        if self.kind == "automatic_renewal" and (self.renewal_amount_mxn is None or not self.renewal_frequency.strip()):
            raise ValidationError("La renovación debe informar importe y periodicidad del cobro.")
        if self.delivered_on and (not self.delivery_channel.strip() or not self.delivery_evidence.strip()):
            raise ValidationError("La entrega necesita canal y evidencia.")

    def __str__(self):
        return f"AV-{self.pk} · {self.title}"

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.published_at:
                for field in ["subscription_id", "kind", "effective_on", "title", "body", "requires_consent", "document_id", "renewal_amount_mxn", "renewal_frequency", "published_at", "notification_id"]:
                    if getattr(original, field) != getattr(self, field):
                        raise ValidationError("Un aviso publicado es inmutable; prepara un nuevo aviso.")
        return super().save(*args, **kwargs)


class RetentionDisposal(models.Model):
    CATEGORY = [("support_ticket_content", "Contenido de tickets resueltos"), ("arco_request_content", "Contenido de solicitudes ARCO concluidas")]
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT)
    category = models.CharField(max_length=40, choices=CATEGORY)
    record_ids = models.JSONField(default=list)
    snapshot_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="disposal_previews")
    performed_at = models.DateTimeField(null=True, blank=True)
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="disposals_performed")
    external_copies_evidence = models.TextField(blank=True)

    def __str__(self):
        return f"DEP-{self.pk} · {self.get_category_display()}"


class PlanRegistration(models.Model):
    plan = models.OneToOneField("core.Plan", on_delete=models.PROTECT, related_name="legal_registration")
    tariff_reference = models.CharField(max_length=200)
    registered_on = models.DateField()
    effective_on = models.DateField()
    expires_on = models.DateField(null=True, blank=True)
    source_url = models.URLField(max_length=700)
    evidence = models.TextField()
    approved = models.BooleanField(default=False)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    plan_snapshot = models.JSONField(default=dict, blank=True, editable=False)

    @staticmethod
    def snapshot(plan):
        return {"organization_id": plan.organization_id, "name": plan.name, "download_mbps": plan.download_mbps, "upload_mbps": plan.upload_mbps, "price_mxn": f"{Decimal(str(plan.price_mxn)):.2f}", "tax_rate": f"{Decimal(str(plan.tax_rate)):.3f}"}

    def clean(self):
        if self.approved and not (self.reviewed_by_id and self.reviewed_at and self.evidence.strip() and self.tariff_reference.strip() and self.plan_snapshot):
            raise ValidationError("La tarifa requiere folio, evidencia, responsable, fecha y copia del plan revisado.")
        if self.registered_on and self.registered_on > timezone.localdate():
            raise ValidationError("La fecha de registro no puede estar en el futuro.")
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.approved:
                for field in ["plan_id", "tariff_reference", "registered_on", "effective_on", "expires_on", "source_url", "evidence", "plan_snapshot"]:
                    if getattr(original, field) != getattr(self, field):
                        raise ValidationError("Revoca primero la revisión vigente antes de sustituir su evidencia.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.plan.name} · {self.tariff_reference}"
