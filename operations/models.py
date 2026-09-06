from decimal import Decimal
from datetime import timedelta
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone


def ticket_folio():
    return f"T-{uuid.uuid4().hex[:12].upper()}"


def complaint_deadline():
    return timezone.now() + timedelta(days=15)


class Ticket(models.Model):
    KIND = [("technical", "Falla técnica"), ("billing", "Aclaración de facturación"), ("general", "Consulta")]
    STATUS = [("open", "Abierto"), ("in_progress", "En atención"), ("resolved", "Resuelto")]
    customer = models.ForeignKey("core.Customer", on_delete=models.PROTECT, related_name="tickets")
    subscription = models.ForeignKey("core.Subscription", on_delete=models.PROTECT, null=True, blank=True)
    folio = models.CharField(max_length=20, unique=True, default=ticket_folio, editable=False)
    kind = models.CharField(max_length=16, choices=KIND, default="technical")
    subject = models.CharField(max_length=200)
    description = models.TextField()
    channel = models.CharField(max_length=30, default="staff")
    status = models.CharField(max_length=16, choices=STATUS, default="open")
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    resolution = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField(default=complaint_deadline)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["customer", "kind", "status"])]

    def clean(self):
        if self.subscription_id and self.subscription.customer_id != self.customer_id:
            raise ValidationError("El servicio debe pertenecer al cliente del ticket.")
        if self.status == "resolved" and not self.resolution.strip():
            raise ValidationError("Registra la resolución antes de cerrar el ticket.")

    def __str__(self):
        return f"{self.folio} · {self.subject}"


class Site(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    name = models.CharField(max_length=150)
    address = models.TextField()
    latitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitude = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    structure_height_m = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    owner_permission = models.TextField(blank=True, help_text="Referencia al contrato de arrendamiento o permiso del propietario.")
    permit_status = models.CharField(max_length=20, choices=[("unreviewed", "Pendiente de revisión"), ("approved", "Documentación verificada"), ("not_required", "No aplica, con fundamento")], default="unreviewed")
    permit_evidence = models.TextField(blank=True, help_text="Folio, autoridad, vigencia y fundamento de permisos aplicables al sitio.")
    permit_expires_on = models.DateField(null=True, blank=True)

    def clean(self):
        if self.latitude is not None and not -90 <= self.latitude <= 90:
            raise ValidationError("Latitud fuera de rango.")
        if self.longitude is not None and not -180 <= self.longitude <= 180:
            raise ValidationError("Longitud fuera de rango.")
        if self.permit_status != "unreviewed" and not self.permit_evidence.strip():
            raise ValidationError("Documenta el permiso o la determinación de no aplicabilidad.")

    def __str__(self):
        return self.name


class Sector(models.Model):
    site = models.ForeignKey(Site, on_delete=models.PROTECT, related_name="sectors")
    name = models.CharField(max_length=100)
    frequency_mhz = models.PositiveIntegerField(null=True, blank=True)
    channel_width_mhz = models.PositiveIntegerField(null=True, blank=True)
    outdoor = models.BooleanField(default=True)
    dfs_required = models.BooleanField(default=False)
    dfs_enabled = models.BooleanField(default=False)
    tpc_required = models.BooleanField(default=False)
    tpc_enabled = models.BooleanField(default=False)
    regulatory_basis = models.TextField(blank=True, help_text="Banda, clase de equipo, límite y disposición técnica vigente.")
    capacity_mbps = models.PositiveIntegerField(default=100)

    def clean(self):
        if self.dfs_required and not self.dfs_enabled:
            raise ValidationError("La banda seleccionada requiere DFS habilitado.")
        if self.tpc_required and not self.tpc_enabled:
            raise ValidationError("La banda seleccionada requiere TPC habilitado.")

    def __str__(self):
        return f"{self.site} / {self.name}"


class Equipment(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    serial_number = models.CharField(max_length=100, unique=True)
    model = models.CharField(max_length=120)
    role = models.CharField(max_length=20, choices=[("cpe", "CPE"), ("ap", "Punto de acceso"), ("backhaul", "Enlace de transporte"), ("router", "Router")], default="cpe")
    sector = models.ForeignKey(Sector, on_delete=models.PROTECT, null=True, blank=True, related_name="equipment")
    subscription = models.ForeignKey("core.Subscription", on_delete=models.PROTECT, null=True, blank=True)
    status = models.CharField(max_length=20, choices=[("stock", "Almacén"), ("installed", "Instalado"), ("repair", "Reparación"), ("retired", "Retirado")], default="stock")
    homologation_certificate = models.CharField(max_length=150, blank=True)
    homologation_verified = models.BooleanField(default=False)
    homologation_evidence = models.TextField(blank=True)
    firmware = models.CharField(max_length=100, blank=True)
    regulatory_profile = models.CharField(max_length=100, default="Mexico")
    tx_power_dbm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    antenna_gain_dbi = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    cable_loss_db = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0"), validators=[MinValueValidator(0)])
    allowed_eirp_dbm = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)

    @property
    def eirp_dbm(self):
        if self.tx_power_dbm is None or self.antenna_gain_dbi is None:
            return None
        return self.tx_power_dbm + self.antenna_gain_dbi - self.cable_loss_db

    def clean(self):
        if self.sector_id and self.sector.site.organization_id != self.organization_id:
            raise ValidationError("El sector pertenece a otro operador.")
        if self.subscription_id and self.subscription.customer.organization_id != self.organization_id:
            raise ValidationError("El servicio pertenece a otro operador.")
        if self.homologation_verified and not (self.homologation_certificate and self.homologation_evidence.strip()):
            raise ValidationError("Se requiere certificado y evidencia de homologación.")
        if self.eirp_dbm is not None and self.allowed_eirp_dbm is not None and self.eirp_dbm > self.allowed_eirp_dbm:
            raise ValidationError("La PIRE calculada excede el límite documentado.")

    def __str__(self):
        return f"{self.model} · {self.serial_number}"


class WorkOrder(models.Model):
    subscription = models.ForeignKey("core.Subscription", on_delete=models.PROTECT, related_name="work_orders")
    kind = models.CharField(max_length=20, choices=[("installation", "Instalación"), ("repair", "Reparación"), ("retrieval", "Retiro")], default="installation")
    status = models.CharField(max_length=20, choices=[("scheduled", "Programada"), ("in_progress", "En ejecución"), ("completed", "Completada"), ("cancelled", "Cancelada")], default="scheduled")
    scheduled_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    notes = models.TextField(blank=True)
    completion_evidence = models.TextField(blank=True, help_text="Identificación del equipo, pruebas realizadas, resultado y aceptación del cliente. No agregues contraseñas.")
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"OT-{self.pk or 'nueva'} · {self.get_kind_display()}"


class Outage(models.Model):
    organization = models.ForeignKey("core.Organization", on_delete=models.PROTECT)
    site = models.ForeignKey(Site, on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField(max_length=200)
    started_at = models.DateTimeField()
    ended_at = models.DateTimeField(null=True, blank=True)
    provider_attributable = models.BooleanField(default=False)
    attribution_evidence = models.TextField(blank=True)
    subscriptions = models.ManyToManyField("core.Subscription", blank=True, related_name="outages")
    notes = models.TextField(blank=True)

    def clean(self):
        if self.ended_at and self.ended_at <= self.started_at:
            raise ValidationError("La recuperación debe ser posterior al inicio de la falla.")
        if self.site_id and self.site.organization_id != self.organization_id:
            raise ValidationError("El sitio pertenece a otro operador.")
        if self.provider_attributable and not self.attribution_evidence.strip():
            raise ValidationError("Registra el fundamento de la atribución de la falla.")

    def __str__(self):
        return self.title


class OutageCredit(models.Model):
    outage = models.ForeignKey(Outage, on_delete=models.PROTECT, related_name="credits")
    subscription = models.ForeignKey("core.Subscription", on_delete=models.PROTECT)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    proportional_amount = models.DecimalField(max_digits=12, decimal_places=2)
    bonus_amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    ledger_reference = models.CharField(max_length=150, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["outage", "subscription", "period_start", "period_end"], name="unique_outage_subscription_credit")]

    @property
    def total(self):
        return self.proportional_amount + self.bonus_amount
