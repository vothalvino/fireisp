import uuid
from django.db import models
from django.db.models import Q
from django.utils import timezone


class FiscalProfile(models.Model):
    organization = models.OneToOneField('core.Organization',on_delete=models.PROTECT)
    username_encrypted = models.TextField(blank=True)
    password_encrypted = models.TextField(blank=True)
    csd_encrypted = models.TextField(blank=True)
    fiel_encrypted = models.TextField(blank=True)
    issuer_rfc = models.CharField(max_length=13,default='EKU9003173C9')
    issuer_name = models.CharField(max_length=254,default='ESCUELA KEMPER URGATE')
    fiscal_regime = models.CharField(max_length=3,default='601')
    postal_code = models.CharField(max_length=5,default='20928')
    environment = models.CharField(max_length=12,default='demo',editable=False)
    verified_at = models.DateTimeField(null=True,blank=True)
    verification_status = models.CharField(max_length=300,blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class FiscalDocument(models.Model):
    invoice = models.ForeignKey('billing.Invoice',null=True,blank=True,on_delete=models.PROTECT,related_name='fiscal_documents')
    global_batch=models.OneToOneField('GlobalBatch',null=True,blank=True,on_delete=models.PROTECT,related_name='document')
    credit_memo=models.OneToOneField('billing.CreditMemo',null=True,blank=True,on_delete=models.PROTECT,related_name='fiscal_document')
    allocation = models.OneToOneField('billing.Allocation',null=True,blank=True,on_delete=models.PROTECT,related_name='fiscal_document')
    kind = models.CharField(max_length=12,choices=[('income','Factura'),('payment','Complemento de pago'),('credit','Nota de crédito'),('global','Factura global')],default='income')
    local_id = models.UUIDField(default=uuid.uuid4,unique=True,editable=False)
    uuid = models.CharField(max_length=36,blank=True,db_index=True)
    status = models.CharField(max_length=20,default='draft',choices=[('draft','Borrador'),('submitting','Enviando'),('stamped','Timbrado'),('error','Rechazado'),('uncertain','Por recuperar'),('cancel_pending','Cancelación pendiente'),('cancelled','Cancelado')])
    payment_method = models.CharField(max_length=3,choices=[('PUE','PUE'),('PPD','PPD')],default='PPD')
    payment_form = models.CharField(max_length=2,default='99')
    request_xml = models.TextField(blank=True)
    xml = models.TextField(blank=True)
    cancellation_xml = models.TextField(blank=True)
    cancellation_reason = models.CharField(max_length=2,blank=True)
    cancellation_replacement = models.CharField(max_length=36,blank=True)
    cancellation_code = models.CharField(max_length=30,blank=True)
    error = models.CharField(max_length=500,blank=True)
    stamped_at = models.DateTimeField(null=True,blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['invoice'],condition=Q(kind='income'),name='one_income_cfdi_per_invoice'),
                       models.CheckConstraint(condition=Q(kind='income',invoice__isnull=False,allocation__isnull=True,global_batch__isnull=True,credit_memo__isnull=True)|Q(kind='payment',invoice__isnull=False,allocation__isnull=False,global_batch__isnull=True,credit_memo__isnull=True)|Q(kind='credit',invoice__isnull=False,allocation__isnull=True,credit_memo__isnull=False,global_batch__isnull=True)|Q(kind='global',invoice__isnull=True,global_batch__isnull=False,allocation__isnull=True,credit_memo__isnull=True),name='cfdi_payment_allocation')]


class FiscalAttempt(models.Model):
    document=models.ForeignKey(FiscalDocument,on_delete=models.PROTECT,related_name='attempts')
    request_xml=models.TextField()
    outcome=models.CharField(max_length=20)
    error=models.CharField(max_length=500,blank=True)
    created_at=models.DateTimeField(default=timezone.now)


class GlobalBatch(models.Model):
    organization=models.ForeignKey('core.Organization',on_delete=models.PROTECT)
    period_start=models.DateField()
    period_end=models.DateField()
    periodicity=models.CharField(max_length=2,default='04')
    idempotency_key=models.CharField(max_length=100,unique=True)
    created_at=models.DateTimeField(default=timezone.now)


class GlobalItem(models.Model):
    batch=models.ForeignKey(GlobalBatch,on_delete=models.PROTECT,related_name='items')
    invoice=models.OneToOneField('billing.Invoice',on_delete=models.PROTECT,related_name='global_item')
