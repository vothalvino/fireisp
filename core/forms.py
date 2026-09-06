from django import forms
from django.contrib.auth import get_user_model
from .models import Branch, Customer, Organization, Plan, Subscription

class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ['name', 'legal_name', 'rfc']
        help_texts = {'rfc': 'Los datos fiscales de demostración se configuran en Finkok.'}

class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['branch', 'name', 'email', 'phone', 'address', 'rfc', 'fiscal_regime', 'fiscal_postal_code', 'invoice_use']
        widgets = {'address': forms.Textarea(attrs={'rows': 3})}
        labels = {'branch': 'Sucursal'}
        help_texts = {'rfc': 'Opcional hasta solicitar factura. No se exige constancia de situación fiscal.',
                      'name': 'Para facturar, usa el nombre exactamente como aparece en el RFC.'}

class BranchForm(forms.ModelForm):
    class Meta:
        model = Branch
        fields = ['name', 'address']

class PlanForm(forms.ModelForm):
    class Meta:
        model = Plan
        fields = ['name', 'download_mbps', 'upload_mbps', 'price_mxn']
        help_texts = {'price_mxn': 'Precio total mensual en MXN, IVA 16% incluido. Internet fijo independiente.'}

class SubscriptionForm(forms.ModelForm):
    class Meta:
        model = Subscription
        fields = ['plan', 'access_username']
        help_texts = {'access_username': 'No contiene la contraseña. Se administra en el módulo de red.'}
    def __init__(self, *args, **kwargs):
        organization = kwargs.pop('organization')
        super().__init__(*args, **kwargs)
        self.fields['plan'].queryset = Plan.objects.filter(organization=organization, is_active=True)

class StaffForm(forms.Form):
    username = forms.RegexField(r'^[a-zA-Z0-9_.@-]+$', max_length=150, label='Usuario')
    first_name = forms.CharField(max_length=150, label='Nombre')
    email = forms.EmailField(label='Correo')
    role = forms.ChoiceField(label='Función', choices=[(v, v) for v in ['Administración', 'Cobranza', 'Red', 'Soporte', 'Cumplimiento']])
    def clean_username(self):
        value = self.cleaned_data['username']
        if get_user_model().objects.filter(username=value).exists(): raise forms.ValidationError('Este usuario ya existe.')
        return value
