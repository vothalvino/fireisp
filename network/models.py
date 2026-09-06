import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Router(models.Model):
    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT)
    name = models.CharField('Nombre', max_length=100)
    management_host = models.GenericIPAddressField('IPv4 de administración', protocol='IPv4', unique=True)
    ssh_port = models.PositiveIntegerField('Puerto SSH', default=22, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    username = models.CharField('Usuario SSH', max_length=64)
    password_encrypted = models.TextField(blank=True)
    radius_secret_encrypted = models.TextField(blank=True)
    is_lab = models.BooleanField('Laboratorio aislado', default=True)
    candidate_host_key = models.TextField(blank=True)
    trusted_host_key = models.TextField(blank=True)
    trusted_at = models.DateTimeField(null=True, blank=True)
    snapshot = models.JSONField(default=dict, blank=True)
    snapshot_hash = models.CharField(max_length=64, blank=True)
    discovered_at = models.DateTimeField(null=True, blank=True)
    provisioned_at = models.DateTimeField(null=True, blank=True)
    readiness = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def prefix(self):
        return f'fi{self.pk}'

    @property
    def service_name(self):
        return f'fireisp-lab-{self.pk}'

    @property
    def readiness_items(self):
        labels = {'configured': 'Recursos propios configurados', 'private_link': 'Enlace privado', 'pppoe_session': 'Sesión PPPoE real', 'rate_limit': 'Velocidad instalada', 'accounting': 'Contabilidad de inicio y fin', 'accounting_interim': 'Actualización intermedia de contabilidad', 'disconnect': 'Desconexión por RADIUS', 'suspension_reject': 'Acceso rechazado al suspender', 'reconnect': 'Reconexión al reactivar', 'plan_change': 'Cambio de velocidad al reconectar', 'local_cache_auth': 'Autenticación con la web detenida', 'accounting_replay': 'Contabilidad recuperada tras la caída', 'end_to_end': 'Prueba completa de laboratorio'}
        return [(labels.get(key, key), value) for key, value in self.readiness.items()]


class ProvisioningJob(models.Model):
    ACTIONS = [('probe', 'Leer huella SSH'), ('discover', 'Descubrir'), ('apply', 'Aplicar plan'), ('rollback', 'Revertir recursos propios'), ('verify', 'Verificar enlace'), ('lab', 'Prueba PPPoE real'), ('disconnect', 'Desconectar sesión')]
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    router = models.ForeignKey(Router, on_delete=models.PROTECT, related_name='jobs')
    action = models.CharField(max_length=16, choices=ACTIONS)
    status = models.CharField(max_length=16, default='pending', choices=[('pending', 'Pendiente'), ('running', 'En ejecución'), ('succeeded', 'Completado'), ('failed', 'Falló')])
    idempotency_key = models.CharField(max_length=160, unique=True)
    plan = models.JSONField(default=dict, blank=True)
    journal = models.JSONField(default=list, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class RadiusCredential(models.Model):
    subscription = models.OneToOneField('core.Subscription', null=True, blank=True, on_delete=models.CASCADE, related_name='radius_credential')
    router = models.ForeignKey(Router, on_delete=models.PROTECT)
    username = models.CharField(max_length=100, unique=True)
    password_encrypted = models.TextField()
    is_lab = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    state_revision = models.PositiveIntegerField(default=0)
    commissioning = models.BooleanField(default=False)
    expires_at = models.DateTimeField(null=True, blank=True)
    download_mbps = models.PositiveIntegerField(default=10)
    upload_mbps = models.PositiveIntegerField(default=5)


class RadiusSession(models.Model):
    router = models.ForeignKey(Router, on_delete=models.PROTECT)
    username = models.CharField(max_length=100)
    session_id = models.CharField(max_length=128)
    framed_ip = models.GenericIPAddressField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    replayed_at = models.DateTimeField(null=True, blank=True)
    stopped_at = models.DateTimeField(null=True, blank=True)
    input_octets = models.PositiveBigIntegerField(default=0)
    output_octets = models.PositiveBigIntegerField(default=0)
    terminate_cause = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['router', 'session_id'], name='radius_router_session_unique')]
