from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q, Sum,OuterRef,Subquery,Value
from django.db.models.functions import Coalesce
from django.utils import timezone


class InvoiceQuerySet(models.QuerySet):
    def with_balances(self):
        amount_field=models.DecimalField(max_digits=12,decimal_places=2)
        paid=Allocation.objects.filter(invoice_id=OuterRef('pk'),payment__reversal__isnull=True).order_by().values('invoice_id').annotate(s=Sum('amount')).values('s')
        credits=CreditMemo.objects.filter(invoice_id=OuterRef('pk')).order_by().values('invoice_id').annotate(s=Sum('amount')).values('s')
        refunds=Refund.objects.filter(credit_memo__invoice_id=OuterRef('pk')).order_by().values('credit_memo__invoice_id').annotate(s=Sum('amount')).values('s')
        def total(query):
            return Coalesce(Subquery(query,output_field=amount_field),Value(Decimal('0.00')),output_field=amount_field)
        return self.annotate(_ledger_paid=total(paid),_ledger_credited=total(credits),_ledger_refunded=total(refunds))


class Invoice(models.Model):
    objects=InvoiceQuerySet.as_manager()
    customer = models.ForeignKey('core.Customer', on_delete=models.PROTECT, related_name='invoices')
    subscription = models.ForeignKey('core.Subscription', null=True, blank=True, on_delete=models.PROTECT, related_name='invoices')
    number = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=240, default='Servicio de internet mensual')
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2)
    tax = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=12, choices=[('open','Pendiente'),('paid','Pagada'),('void','Anulada')], default='open')
    fiscal_method = models.CharField(max_length=3, choices=[('PUE','PUE'),('PPD','PPD')], default='PPD')
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.UniqueConstraint(fields=['subscription','period_start'], name='unique_subscription_period'),
                       models.CheckConstraint(condition=Q(total__gte=0) & Q(subtotal__gte=0) & Q(tax__gte=0), name='invoice_positive_totals'),
                       models.CheckConstraint(condition=Q(period_end__isnull=True) | Q(period_end__gt=models.F('period_start')), name='invoice_ordered_period')]

    def __str__(self):
        return self.number

    @property
    def paid_amount(self):
        if hasattr(self,'_ledger_paid'):
            return self._ledger_paid.quantize(Decimal('.01'))
        return (self.allocations.filter(payment__reversal__isnull=True).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')).quantize(Decimal('.01'))

    @property
    def balance(self):
        credited=self._ledger_credited if hasattr(self,'_ledger_credited') else self.credit_memos.aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        refunded=self._ledger_refunded if hasattr(self,'_ledger_refunded') else Refund.objects.filter(credit_memo__invoice=self).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
        return (self.total - self.paid_amount - credited + refunded).quantize(Decimal('.01'))


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError('El libro de cobros no permite modificar asientos; usa reversiones.')

    def delete(self):
        raise ValidationError('El libro de cobros no permite eliminar asientos.')


class ImmutableEntry(models.Model):
    objects = ImmutableQuerySet.as_manager()
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError('El asiento es inmutable. Registra una reversión para corregirlo.')
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError('No se pueden eliminar asientos del libro de cobros.')


class Payment(ImmutableEntry):
    METHODS = [('cash','Efectivo'),('transfer','Transferencia'),('card','Tarjeta')]
    customer = models.ForeignKey('core.Customer', on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    method = models.CharField(max_length=12, choices=METHODS)
    reference = models.CharField(max_length=160, blank=True)
    paid_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name='recorded_payments')
    idempotency_key = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ['-created_at', '-pk']
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_payment')]

    @property
    def available(self):
        if hasattr(self, 'reversal'):
            return Decimal('0.00')
        return (self.amount - (self.allocations.aggregate(total=Sum('amount'))['total'] or Decimal('0.00'))).quantize(Decimal('.01'))


class Allocation(ImmutableEntry):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name='allocations')
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name='allocations')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='positive_allocation')]


class PaymentReversal(ImmutableEntry):
    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name='reversal')
    reason = models.CharField(max_length=500)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(default=timezone.now)


class CashClosure(ImmutableEntry):
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    closed_at = models.DateTimeField(default=timezone.now)
    expected = models.DecimalField(max_digits=12, decimal_places=2)
    counted = models.DecimalField(max_digits=12, decimal_places=2)
    difference = models.DecimalField(max_digits=12, decimal_places=2)
    notes = models.TextField(blank=True)


class CashClosureItem(ImmutableEntry):
    closure = models.ForeignKey(CashClosure, on_delete=models.PROTECT, related_name='items')
    payment = models.OneToOneField(Payment, null=True, blank=True, on_delete=models.PROTECT, related_name='closure_item')
    reversal = models.OneToOneField(PaymentReversal, null=True, blank=True, on_delete=models.PROTECT, related_name='closure_item')
    refund = models.OneToOneField('Refund', null=True, blank=True, on_delete=models.PROTECT, related_name='closure_item')

    class Meta:
        constraints = [models.CheckConstraint(condition=(Q(payment__isnull=False,reversal__isnull=True,refund__isnull=True) | Q(payment__isnull=True,reversal__isnull=False,refund__isnull=True) | Q(payment__isnull=True,reversal__isnull=True,refund__isnull=False)), name='one_cash_entry')]


class BankEntry(models.Model):
    organization = models.ForeignKey('core.Organization', on_delete=models.PROTECT)
    account = models.CharField(max_length=80)
    external_reference = models.CharField(max_length=160)
    date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    customer_code = models.CharField(max_length=80, blank=True)
    description = models.CharField(max_length=500, blank=True)
    fingerprint = models.CharField(max_length=64, unique=True)
    payment = models.OneToOneField(Payment, null=True, blank=True, on_delete=models.PROTECT)
    imported_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['-date','-pk']
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name='bank_credit_positive')]


class CreditMemo(ImmutableEntry):
    invoice=models.ForeignKey(Invoice,on_delete=models.PROTECT,related_name='credit_memos')
    amount=models.DecimalField(max_digits=12,decimal_places=2)
    reason=models.CharField(max_length=500)
    source_key=models.CharField(max_length=100,unique=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)

    class Meta:
        constraints=[models.CheckConstraint(condition=Q(amount__gt=0),name='credit_memo_positive')]


class Refund(ImmutableEntry):
    credit_memo=models.ForeignKey(CreditMemo,on_delete=models.PROTECT,related_name='refunds')
    amount=models.DecimalField(max_digits=12,decimal_places=2)
    method=models.CharField(max_length=12,choices=Payment.METHODS)
    reference=models.CharField(max_length=160)
    idempotency_key=models.CharField(max_length=100,unique=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)

    class Meta:
        constraints=[models.CheckConstraint(condition=Q(amount__gt=0),name='refund_positive')]


class SuspensionPolicy(models.Model):
    organization=models.OneToOneField('core.Organization',on_delete=models.PROTECT)
    automatic_enabled=models.BooleanField(default=False)
    grace_hours=models.PositiveIntegerField(default=24)
    updated_at=models.DateTimeField(auto_now=True)


class SuspensionProposal(ImmutableEntry):
    subscription=models.ForeignKey('core.Subscription',on_delete=models.PROTECT,related_name='suspension_proposals')
    snapshot_paid_until=models.DateTimeField()
    reason=models.CharField(max_length=30,default='nonpayment')
    idempotency_key=models.CharField(max_length=100,unique=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)


class SuspensionDecision(ImmutableEntry):
    proposal=models.OneToOneField(SuspensionProposal,on_delete=models.PROTECT,related_name='decision')
    approved=models.BooleanField()
    note=models.CharField(max_length=500)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)


class SuspensionApplication(ImmutableEntry):
    proposal=models.OneToOneField(SuspensionProposal,on_delete=models.PROTECT,related_name='application')
    applied=models.BooleanField()
    previous_status=models.CharField(max_length=20)
    resulting_status=models.CharField(max_length=20)
    detail=models.CharField(max_length=500)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)


class SuspensionRelease(ImmutableEntry):
    application=models.OneToOneField(SuspensionApplication,on_delete=models.PROTECT,related_name='release')
    reason=models.CharField(max_length=100,default='paid_renewal')
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.PROTECT)
    created_at=models.DateTimeField(default=timezone.now)
