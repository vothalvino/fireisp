from django import forms
import uuid
from billing.models import Invoice
from .models import FiscalProfile


class ProfileForm(forms.ModelForm):
    username=forms.CharField(label='Usuario del token DEMO',required=False,widget=forms.PasswordInput(render_value=False),help_text='Deja vacío para conservar el usuario guardado.')
    password=forms.CharField(label='Token DEMO',required=False,widget=forms.PasswordInput(render_value=False),help_text='Se guarda cifrado y nunca se vuelve a mostrar.')
    csd_zip=forms.FileField(label='CSD DEMO (.zip con .cer y .key)',required=False)
    fiel_zip=forms.FileField(label='FIEL DEMO para cancelación (.zip)',required=False)
    certificate_password=forms.CharField(label='Contraseña de certificados DEMO',required=False,widget=forms.PasswordInput)
    class Meta:
        model=FiscalProfile
        fields=['issuer_rfc','issuer_name','fiscal_regime','postal_code','username','password','csd_zip','fiel_zip','certificate_password']
        labels={'issuer_rfc':'RFC emisor de pruebas','issuer_name':'Razón social del emisor','fiscal_regime':'Régimen fiscal','postal_code':'Código postal de expedición'}


class IssueForm(forms.Form):
    method=forms.ChoiceField(label='Método fiscal',choices=[('PPD','PPD · parcialidades o diferido'),('PUE','PUE · pagado en una exhibición')])
    payment_form=forms.ChoiceField(label='Forma de pago',choices=[('99','99 · Por definir (PPD)'),('01','01 · Efectivo'),('03','03 · Transferencia'),('04','04 · Tarjeta de crédito'),('28','28 · Tarjeta de débito')])


class CancellationForm(forms.Form):
    reason=forms.ChoiceField(label='Motivo de cancelación',choices=[('02','02 · Error sin relación'),('01','01 · Error con sustitución'),('03','03 · Operación no realizada'),('04','04 · Operación en factura global')])
    replacement=forms.UUIDField(label='UUID sustituto (motivo 01)',required=False)


class GlobalForm(forms.Form):
    invoices=forms.ModelMultipleChoiceField(queryset=Invoice.objects.filter(customer__rfc='XAXX010101000',status='paid',global_item__isnull=True).exclude(fiscal_documents__kind='income'),label='Operaciones liquidadas de público en general')
    period_start=forms.DateField(label='Inicio del periodo',widget=forms.DateInput(attrs={'type':'date'}))
    period_end=forms.DateField(label='Fin del periodo',widget=forms.DateInput(attrs={'type':'date'}))
    periodicity=forms.ChoiceField(label='Periodicidad',choices=[('01','Diaria'),('02','Semanal'),('03','Quincenal'),('04','Mensual')],initial='04')
    payment_form=forms.ChoiceField(label='Forma de pago de mayor importe',choices=[('01','Efectivo'),('03','Transferencia'),('04','Tarjeta de crédito'),('28','Tarjeta de débito')])
    idempotency_key=forms.CharField(widget=forms.HiddenInput)

    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.fields['idempotency_key'].initial=str(uuid.uuid4())
