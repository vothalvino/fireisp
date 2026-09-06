from django import forms
from core.secrets import encrypt
from .models import Router


class RouterForm(forms.ModelForm):
    password = forms.CharField(label='Contraseña SSH', widget=forms.PasswordInput, required=True)

    class Meta:
        model = Router
        fields = ['organization', 'name', 'management_host', 'ssh_port', 'username', 'password', 'is_lab']

    def save(self, commit=True):
        router = super().save(commit=False)
        router.password_encrypted = encrypt(self.cleaned_data['password'])
        if commit:
            router.save()
        return router


class ReviewForm(forms.Form):
    snapshot_hash = forms.CharField(widget=forms.HiddenInput)
    approve = forms.BooleanField(label='Apruebo crear solamente los recursos FireISP descritos en este plan.')
    approve_global_ppp = forms.BooleanField(required=False, label='Apruebo los cambios globales PPP/RADIUS que indica el plan; pueden afectar otros servicios PPP.')


class TrustForm(forms.Form):
    fingerprint = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(label='Revisé la huella SSH y confío en esta identidad del router.')


class SubscriptionAccessForm(forms.Form):
    from core.models import Subscription
    subscription = forms.ModelChoiceField(label='Suscripción', queryset=Subscription.objects.select_related('customer', 'plan').all())
    router = forms.ModelChoiceField(label='Router', queryset=Router.objects.all())
    password = forms.CharField(label='Contraseña PPPoE', min_length=12, max_length=128, widget=forms.PasswordInput)
    commissioning = forms.BooleanField(required=False, label='Autorizar puesta en servicio por 2 horas sin iniciar facturación (solo suscripción pendiente).')
    disconnect_current = forms.BooleanField(required=False, label='Desconectar la sesión actual para aplicar la contraseña o el plan al reconectar.')
