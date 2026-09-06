import uuid
from decimal import Decimal
from django import forms
from core.models import Customer
from .models import Payment,Invoice,SuspensionPolicy


class PaymentForm(forms.Form):
    customer = forms.ModelChoiceField(Customer.objects.all(),label='Cliente')
    amount = forms.DecimalField(label='Importe MXN',max_digits=12,decimal_places=2,min_value=Decimal('.01'))
    method = forms.ChoiceField(label='Forma de cobro',choices=Payment.METHODS)
    reference = forms.CharField(label='Referencia',max_length=160,required=False)
    idempotency_key = forms.CharField(widget=forms.HiddenInput)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['idempotency_key'].initial = str(uuid.uuid4())


class ReversalForm(forms.Form):
    reason = forms.CharField(label='Motivo de reversión',max_length=500,widget=forms.Textarea)


class CashClosureForm(forms.Form):
    counted = forms.DecimalField(label='Efectivo contado MXN',max_digits=12,decimal_places=2,min_value=0)
    notes = forms.CharField(label='Observaciones',required=False,widget=forms.Textarea)


class BankImportForm(forms.Form):
    account = forms.CharField(label='Cuenta bancaria (alias)',max_length=80)
    file = forms.FileField(label='Estado de cuenta CSV',help_text='UTF-8. Columnas: external_reference,date,amount,customer_code,description. Sólo abonos; fechas AAAA-MM-DD.')


class ReconcileForm(forms.Form):
    customer = forms.ModelChoiceField(Customer.objects.all(),label='Cliente que realizó el depósito')


class CreditMemoForm(forms.Form):
    invoice=forms.ModelChoiceField(Invoice.objects.exclude(status='void'),label='Mensualidad')
    amount=forms.DecimalField(label='Ajuste a favor del cliente MXN',max_digits=12,decimal_places=2,min_value=Decimal('.01'))
    reason=forms.CharField(label='Motivo',max_length=500)
    source_key=forms.CharField(widget=forms.HiddenInput)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['source_key'].initial='manual:'+str(uuid.uuid4())


class RefundForm(forms.Form):
    amount=forms.DecimalField(label='Devolución efectivamente entregada MXN',max_digits=12,decimal_places=2,min_value=Decimal('.01'))
    method=forms.ChoiceField(label='Forma de devolución',choices=Payment.METHODS)
    reference=forms.CharField(label='Referencia / comprobante de salida',max_length=160)
    idempotency_key=forms.CharField(widget=forms.HiddenInput)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['idempotency_key'].initial='refund:'+str(uuid.uuid4())


class SuspensionPolicyForm(forms.ModelForm):
    class Meta:
        model=SuspensionPolicy
        fields=['automatic_enabled','grace_hours']
        labels={'automatic_enabled':'Habilitar suspensiones automáticas por falta de pago','grace_hours':'Horas de gracia después del vencimiento'}
        help_texts={'automatic_enabled':'Desactivado de inicio. Toda aplicación vuelve a verificar vigencia, aclaraciones y sincronización reciente de red.'}


class SuspensionReviewForm(forms.Form):
    approved=forms.TypedChoiceField(label='Decisión',choices=[('yes','Aprobar suspensión'),('no','Rechazar propuesta')],coerce=lambda value:value=='yes')
    note=forms.CharField(label='Justificación de la revisión',max_length=500,widget=forms.Textarea)
