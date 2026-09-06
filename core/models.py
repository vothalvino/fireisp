import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone

rfc_validator = RegexValidator(r'^[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}$', 'RFC inválido.')

class Organization(models.Model):
    name = models.CharField('Nombre comercial', max_length=160)
    legal_name = models.CharField('Razón social', max_length=254, blank=True)
    rfc = models.CharField('RFC', max_length=13, blank=True, validators=[rfc_validator])
    timezone = models.CharField('Zona horaria', max_length=80, default='America/Chihuahua')
    currency = models.CharField('Moneda', max_length=3, default='MXN')
    demo_mode = models.BooleanField('Entorno de demostración', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    def __str__(self): return self.name

class Branch(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    name = models.CharField('Sucursal', max_length=120)
    address = models.TextField('Dirección', blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['organization', 'name'], name='unique_branch_name')]
        ordering = ['name']
    def __str__(self): return self.name

class Customer(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    branch = models.ForeignKey(Branch, null=True, blank=True, on_delete=models.PROTECT)
    code = models.CharField('Número de cliente', max_length=32, unique=True)
    name = models.CharField('Nombre / razón social', max_length=254)
    email = models.EmailField('Correo electrónico', blank=True)
    phone = models.CharField('Teléfono', max_length=25, blank=True)
    address = models.TextField('Domicilio de servicio')
    rfc = models.CharField('RFC', max_length=13, blank=True, validators=[rfc_validator])
    fiscal_regime = models.CharField('Régimen fiscal', max_length=3, blank=True)
    fiscal_postal_code = models.CharField('Código postal fiscal', max_length=5, blank=True,
        validators=[RegexValidator(r'^\d{5}$', 'Escribe cinco dígitos.')])
    invoice_use = models.CharField('Uso CFDI', max_length=4, default='S01')
    is_active = models.BooleanField('Activo', default=True)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['name']), models.Index(fields=['phone'])]
    def clean(self):
        if self.branch_id and self.branch.organization_id != self.organization_id:
            raise ValidationError('La sucursal pertenece a otra organización.')
    def __str__(self): return f'{self.code} · {self.name}'

class Plan(models.Model):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    name = models.CharField('Nombre del plan', max_length=120)
    download_mbps = models.PositiveIntegerField('Descarga (Mbps)', validators=[MinValueValidator(1)])
    upload_mbps = models.PositiveIntegerField('Subida (Mbps)', validators=[MinValueValidator(1)])
    price_mxn = models.DecimalField('Mensualidad con IVA (MXN)', max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    tax_rate = models.DecimalField('Tasa IVA', max_digits=4, decimal_places=3, default=Decimal('0.160'))
    is_active = models.BooleanField('Disponible', default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta:
        constraints = [models.CheckConstraint(condition=models.Q(price_mxn__gt=0), name='plan_positive_price'),
                       models.CheckConstraint(condition=models.Q(tax_rate__gte=0, tax_rate__lte=1), name='plan_tax_range')]
    def __str__(self): return f'{self.name} · ${self.price_mxn}'

class Subscription(models.Model):
    STATUS = [('pending', 'Pendiente de instalación'), ('active', 'Activo'), ('suspended', 'Suspendido'), ('cancelled', 'Cancelado')]
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='subscriptions')
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    status = models.CharField('Estado', max_length=20, choices=STATUS, default='pending')
    activated_at = models.DateTimeField('Activación real', null=True, blank=True)
    paid_until = models.DateTimeField('Pagado hasta', null=True, blank=True)
    access_username = models.CharField('Usuario PPPoE', max_length=64, unique=True,
        validators=[RegexValidator(r'^[A-Za-z0-9_.@-]+$', 'Usa letras, números, punto, guion, @ o _.')])
    created_at = models.DateTimeField(auto_now_add=True)
    public_id = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    class Meta:
        indexes = [models.Index(fields=['status', 'paid_until'])]
    def clean(self):
        if self.customer_id and self.plan_id and self.customer.organization_id != self.plan.organization_id:
            raise ValidationError('El plan pertenece a otra organización.')
    def __str__(self): return f'{self.customer.code} / {self.plan.name}'

class AuditEvent(models.Model):
    at = models.DateTimeField(default=timezone.now, db_index=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100)
    target = models.CharField(max_length=200)
    details = models.JSONField(default=dict)
    @property
    def display_action(self):
        labels = {'account.login': 'Inicio de sesión', 'account.invited': 'Invitación de acceso creada',
                  'account.activated': 'Cuenta activada', 'customer.created': 'Cliente registrado',
                  'customer.updated': 'Datos del cliente actualizados', 'subscription.created': 'Servicio registrado',
                  'subscription.activated': 'Activación del servicio', 'subscription.cancelled': 'Cancelación registrada',
                  'billing.payment.received': 'Pago registrado', 'billing.invoice.created': 'Periodo de servicio creado',
                  'work_order.completed': 'Orden de trabajo completada', 'organization.updated': 'Datos del ISP actualizados',
                  'network.discover.succeeded': 'Descubrimiento del router completado', 'network.discover.queued': 'Descubrimiento del router solicitado',
                  'network.probe.succeeded': 'Identidad SSH consultada', 'network.apply.succeeded': 'Configuración de red verificada',
                  'network.lab.succeeded': 'Prueba PPPoE completada', 'network.lab.failed': 'Prueba de red por revisar'}
        return labels.get(self.action, self.action)
    class Meta:
        ordering = ['-at']
    def save(self, *args, **kwargs):
        if self.pk: raise ValidationError('La bitácora es inmutable.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs): raise ValidationError('La bitácora es inmutable.')

class ActivationToken(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    digest = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True)

class OutboxEvent(models.Model):
    key = models.CharField(max_length=200, unique=True)
    topic = models.CharField(max_length=80)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    available_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=200, blank=True)

class Notification(models.Model):
    customer = models.ForeignKey(Customer, null=True, on_delete=models.CASCADE)
    title = models.CharField(max_length=160)
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True)
    source = models.OneToOneField(OutboxEvent, null=True, on_delete=models.PROTECT)

class HealthCheck(models.Model):
    code = models.CharField(max_length=60, unique=True)
    status = models.CharField(max_length=20)
    details = models.JSONField(default=dict)
    checked_at = models.DateTimeField(auto_now=True)
