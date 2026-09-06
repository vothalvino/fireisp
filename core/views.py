import hashlib
import secrets
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login
from django.contrib.auth.forms import SetPasswordForm
from django.contrib.auth.models import Group
from django.contrib.auth.views import LoginView as DjangoLoginView
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import connection, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .forms import BranchForm, CustomerForm, OrganizationForm, PlanForm, StaffForm, SubscriptionForm
from .models import ActivationToken, AuditEvent, Branch, Customer, Notification, Organization, Plan, Subscription
from .security import staff_required
from .services import audit, invite


def reserve_login_attempt(key, limit):
    """Reserve before checking a password; failed requests cannot race past the limit."""
    for _ in range(3):
        if cache.add(key, 1, timeout=900):
            return True
        try:
            count = cache.incr(key)
        except ValueError:
            # The fixed window may expire between add() and incr().
            continue
        if count == 1:
            # Redis may recreate a key if it expired during incr()'s existence check.
            cache.touch(key, timeout=900)
        return count <= limit
    return False


class LoginView(DjangoLoginView):
    template_name = 'registration/login.html'
    redirect_authenticated_user = True
    def post(self, request, *args, **kwargs):
        raw = request.POST.get('username', '').strip()
        user_model = get_user_model()
        username_limit = user_model._meta.get_field(user_model.USERNAME_FIELD).max_length or 254
        # Match AuthenticationForm's normalization and its oversized-input guard.
        if len(raw) <= username_limit:
            raw = user_model.normalize_username(raw)
        self.throttle_key = 'login:' + hashlib.sha256(raw.lower().encode()).hexdigest()
        address = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[-1].strip()
        source_key = 'login-source:' + hashlib.sha256(address.encode()).hexdigest()
        if not reserve_login_attempt(source_key, 80) or not reserve_login_attempt(self.throttle_key, 10):
            return self.rate_limited_response()
        return super().post(request, *args, **kwargs)

    def rate_limited_response(self):
        # Rendering a bound AuthenticationForm calls full_clean() and authenticates.
        # A blocked request must never evaluate (or reveal the validity of) a password.
        kwargs = self.get_form_kwargs()
        kwargs.pop('data', None)
        kwargs.pop('files', None)
        form = self.get_form_class()(**kwargs)
        response = self.render_to_response(self.get_context_data(form=form, rate_limited=True), status=429)
        response['Retry-After'] = '900'
        return response

    def form_valid(self, form):
        cache.delete(self.throttle_key)
        result = super().form_valid(form)
        audit(self.request.user, 'account.login', self.request.user.pk)
        return result
    def get_success_url(self):
        if not self.request.user.is_staff: return '/portal/'
        return super().get_success_url()

def health(request):
    try:
        with connection.cursor() as cursor: cursor.execute('SELECT 1')
        if settings.FIREISP_RELEASE != 'development':
            from .runtime import heartbeat
            heartbeat('web')
        return JsonResponse({'application_ready': True, 'database_ready': True, 'version': settings.FIREISP_VERSION})
    except Exception:
        return JsonResponse({'application_ready': False, 'database_ready': False}, status=503)

def activate_account(request, token):
    digest = hashlib.sha256(token.encode()).hexdigest()
    item = ActivationToken.objects.filter(digest=digest, used_at__isnull=True, expires_at__gt=timezone.now()).select_related('user').first()
    if not item: return render(request, 'registration/expired.html', status=410)
    form = SetPasswordForm(item.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            item = ActivationToken.objects.select_for_update().get(pk=item.pk)
            if item.used_at or item.expires_at <= timezone.now(): return render(request, 'registration/expired.html', status=410)
            user = form.save()
            user.is_active = True
            user.save(update_fields=['is_active'])
            item.used_at = timezone.now()
            item.save(update_fields=['used_at'])
            audit(user, 'account.activated', user.pk)
        login(request, user)
        return redirect('core:dashboard' if user.is_staff else 'core:portal')
    return render(request, 'registration/activate.html', {'form': form, 'account': item.user})

@staff_required
def dashboard(request):
    from operations.models import Ticket, WorkOrder
    from compliance.services import legal_readiness
    org = Organization.objects.first()
    context = {'customer_count': Customer.objects.filter(is_active=True).count(),
               'active_count': Subscription.objects.filter(status='active').count(),
               'pending_count': Subscription.objects.filter(status='pending').count(),
               'customers': Customer.objects.all()[:5], 'recent_events': AuditEvent.objects.all()[:6],
               'readiness': legal_readiness(org) if org else None}
    # Module states remain visible through their dedicated queues; counts do not imply readiness.
    context['ticket_count'] = Ticket.objects.exclude(status__in=['resolved', 'closed']).count()
    context['work_count'] = WorkOrder.objects.exclude(status__in=['completed', 'cancelled']).count()
    return render(request, 'core/dashboard.html', context)

@staff_required
def customers(request):
    query = request.GET.get('q', '').strip()[:120]
    rows = Customer.objects.select_related('branch')
    if query: rows = rows.filter(Q(name__icontains=query) | Q(code__icontains=query) | Q(phone__icontains=query) | Q(email__icontains=query))
    return render(request, 'core/customers.html', {'page': Paginator(rows, 30).get_page(request.GET.get('page')), 'query': query})

def lookup(request, kind):
    query = request.GET.get('q', '').strip()[:120]
    if kind == 'customer':
        rows = Customer.objects.filter(is_active=True)
        if not request.user.is_staff: rows = rows.filter(user=request.user)
        if query: rows = rows.filter(Q(name__icontains=query) | Q(code__icontains=query))
    elif kind == 'subscription':
        rows = Subscription.objects.select_related('customer', 'plan')
        if not request.user.is_staff: rows = rows.filter(customer__user=request.user)
        if query: rows = rows.filter(Q(customer__name__icontains=query) | Q(customer__code__icontains=query) | Q(access_username__icontains=query))
    else: return JsonResponse({'results': []}, status=404)
    return JsonResponse({'results': [{'id': row.pk, 'label': str(row)} for row in rows.order_by('pk')[:25]]})

@staff_required
def customer_create(request):
    org = get_object_or_404(Organization)
    form = CustomerForm(request.POST or None, instance=Customer(organization=org))
    form.fields['branch'].queryset = Branch.objects.filter(organization=org)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            customer = form.save(commit=False)
            customer.organization = org
            customer.code = 'F-' + secrets.token_hex(4).upper()
            customer.full_clean()
            customer.save()
            audit(request.user, 'customer.created', customer.pk)
        messages.success(request, 'Cliente registrado. Puedes agregar su servicio y orden de instalación.')
        return redirect('core:customer_detail', pk=customer.pk)
    return render(request, 'form.html', {'form': form, 'title': 'Nuevo cliente', 'description': 'Comienza con los datos de contacto y del domicilio de servicio.', 'back_url': '/customers/'})

@staff_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    return render(request, 'core/customer_detail.html', {'customer': customer, 'subscriptions': customer.subscriptions.select_related('plan'),
        'notices': Notification.objects.filter(customer=customer)[:5]})

@staff_required
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = CustomerForm(request.POST or None, instance=customer)
    form.fields['branch'].queryset = Branch.objects.filter(organization=customer.organization)
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit(request.user, 'customer.updated', pk, {'fields': form.changed_data})
        messages.success(request, 'Datos actualizados.')
        return redirect('core:customer_detail', pk=pk)
    return render(request, 'form.html', {'form': form, 'title': 'Editar cliente', 'back_url': f'/customers/{pk}/'})

@staff_required
def subscription_create(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    form = SubscriptionForm(request.POST or None, organization=customer.organization)
    if request.method == 'POST' and form.is_valid():
        sub = form.save(commit=False)
        sub.customer = customer
        sub.full_clean()
        sub.save()
        audit(request.user, 'subscription.created', sub.pk)
        messages.success(request, 'Servicio creado. La vigencia inicia al confirmar la instalación.')
        return redirect('core:customer_detail', pk=pk)
    return render(request, 'form.html', {'form': form, 'title': 'Agregar servicio', 'description': str(customer), 'back_url': f'/customers/{pk}/'})

@staff_required
@require_POST
def customer_invite(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    with transaction.atomic():
        customer = Customer.objects.select_for_update().get(pk=pk)
        if not customer.user:
            user = get_user_model().objects.create_user(username=f'cliente-{customer.code.lower()}', email=customer.email, first_name=customer.name[:150], is_active=False)
            customer.user = user
            customer.save(update_fields=['user'])
        token = invite(customer.user, request.user)
    return render(request, 'core/invitation.html', {'activation_url': request.build_absolute_uri(f'/activate/{token}/'), 'account': customer.user, 'customer': customer})

@staff_required
def plans(request):
    return render(request, 'core/plans.html', {'plans': Plan.objects.all()})

@staff_required
def plan_create(request):
    form = PlanForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        plan = form.save(commit=False)
        plan.organization = get_object_or_404(Organization)
        plan.full_clean()
        plan.save()
        audit(request.user, 'plan.created', plan.pk)
        messages.success(request, 'Plan creado. Publica y registra la tarifa antes de ofrecerla comercialmente.')
        return redirect('core:plans')
    return render(request, 'form.html', {'form': form, 'title': 'Nuevo plan', 'description': 'Los planes contratados conservan su precio. Crea una nueva versión para cambios comerciales.', 'back_url': '/plans/'})

@staff_required
def settings_view(request):
    org = get_object_or_404(Organization)
    form = OrganizationForm(request.POST or None, instance=org)
    if request.method == 'POST' and form.is_valid():
        form.save()
        audit(request.user, 'organization.updated', org.pk, {'fields': form.changed_data})
        messages.success(request, 'Organización actualizada.')
        return redirect('core:settings')
    return render(request, 'core/settings.html', {'form': form, 'branches': Branch.objects.all(), 'staff': get_user_model().objects.filter(is_staff=True)})

@staff_required
def system_health(request):
    from .models import HealthCheck, OutboxEvent, RuntimeNode
    from network.models import Router, ProvisioningJob
    from fiscal.models import FiscalProfile
    checks = list(HealthCheck.objects.all())
    for check in checks:
        max_age = timedelta(seconds=120) if check.code.startswith('network_sync') else {'backup': timedelta(minutes=45), 'offsite': timedelta(days=1)}.get(check.code, timedelta(days=7))
        check.stale = check.checked_at < timezone.now() - max_age
    nodes = list(RuntimeNode.objects.all())
    for node in nodes:
        node.stale = node.last_seen < timezone.now() - timedelta(seconds=90)
        node.role_label = {'web': 'Aplicación', 'worker': 'Eventos', 'billing': 'Cobranza',
                           'fiscal': 'Facturación fiscal', 'scheduler': 'Programación', 'network': 'Red'}.get(node.role, node.role)
    return render(request, 'core/system_health.html', {'checks': checks, 'routers': Router.objects.all(), 'profile': FiscalProfile.objects.first(),
        'runtime_nodes': nodes,
        'failed_jobs': ProvisioningJob.objects.filter(status='failed').count(),
        'exhausted_events': OutboxEvent.objects.filter(delivered_at__isnull=True, attempts__gte=5).count()})

@staff_required
def branch_create(request):
    form = BranchForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        branch = form.save(commit=False)
        branch.organization = get_object_or_404(Organization)
        branch.full_clean()
        branch.save()
        audit(request.user, 'branch.created', branch.pk)
        return redirect('core:settings')
    return render(request, 'form.html', {'form': form, 'title': 'Agregar sucursal', 'back_url': '/settings/'})

@staff_required
def staff_create(request):
    form = StaffForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            values = {key: form.cleaned_data[key] for key in ('username', 'first_name', 'email')}
            user = get_user_model().objects.create_user(**values, is_staff=True, is_active=False)
            user.groups.add(Group.objects.get_or_create(name=form.cleaned_data['role'])[0])
            token = invite(user, request.user)
        return render(request, 'core/invitation.html', {'activation_url': request.build_absolute_uri(f'/activate/{token}/'), 'account': user})
    return render(request, 'form.html', {'form': form, 'title': 'Invitar colaborador', 'back_url': '/settings/'})

@staff_required
def audit_view(request):
    rows = AuditEvent.objects.select_related('actor')
    query = request.GET.get('q', '').strip()[:100]
    if query: rows = rows.filter(Q(action__icontains=query) | Q(target__icontains=query))
    return render(request, 'core/audit.html', {'page': Paginator(rows, 50).get_page(request.GET.get('page')), 'query': query})

def _portal_customer(request):
    return get_object_or_404(Customer, user=request.user, is_active=True)

def portal(request):
    customer = _portal_customer(request)
    return render(request, 'core/portal.html', {'customer': customer, 'subscriptions': customer.subscriptions.select_related('plan'),
        'notifications': Notification.objects.filter(customer=customer)[:20]})

def portal_cancel(request, pk):
    customer = _portal_customer(request)
    sub = get_object_or_404(Subscription, customer=customer, pk=pk)
    from django import forms
    class CancelForm(forms.Form):
        reason = forms.CharField(label='Motivo (opcional)', required=False, max_length=1000, widget=forms.Textarea(attrs={'rows': 3}))
        confirm = forms.BooleanField(label='Solicito cancelar este servicio')
    form = CancelForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        from compliance.services import cancel_subscription
        result = cancel_subscription(sub.pk, request.user, 'portal', form.cleaned_data['reason'])
        messages.success(request, f'Solicitud registrada. Folio: {result.folio}.')
        return redirect('core:portal')
    return render(request, 'form.html', {'form': form, 'title': 'Cancelar servicio', 'description': 'Recibirás un folio de seguimiento. Conservas acceso a tus documentos y aclaraciones.', 'back_url': '/portal/', 'submit_label': 'Solicitar cancelación'})

def portal_payments(request):
    from billing.models import Invoice, Payment
    from fiscal.models import FiscalDocument
    customer = _portal_customer(request)
    return render(request, 'core/portal_payments.html', {'invoices': Invoice.objects.with_balances().filter(customer=customer).order_by('-created_at')[:100],
        'payments': Payment.objects.filter(customer=customer).order_by('-created_at')[:100],
        'documents': FiscalDocument.objects.defer('pdf_content').filter(invoice__customer=customer).exclude(uuid='').order_by('-created_at')[:100]})

def portal_document(request, pk, format):
    from fiscal.models import FiscalDocument
    from fiscal.views import download
    get_object_or_404(FiscalDocument.objects.defer('pdf_content'), pk=pk, invoice__customer=_portal_customer(request))
    return download(request, pk, format)

def portal_support(request):
    from django import forms
    from operations.models import Ticket
    customer = _portal_customer(request)
    class SupportForm(forms.ModelForm):
        class Meta:
            model = Ticket
            fields = ['subscription', 'kind', 'subject', 'description']
            labels = {'subscription': 'Servicio', 'kind': 'Tipo de solicitud', 'subject': 'Asunto', 'description': 'Cuéntanos qué pasó'}
            widgets = {'description': forms.Textarea(attrs={'rows': 4})}
    form = SupportForm(request.POST or None, instance=Ticket(customer=customer, channel='portal'))
    form.fields['subscription'].queryset = customer.subscriptions.all()
    if request.method == 'POST' and form.is_valid():
        ticket = form.save(commit=False)
        ticket.customer = customer
        ticket.channel = 'portal'
        ticket.full_clean()
        ticket.save()
        audit(request.user, 'ticket.created', ticket.pk, {'channel': 'portal', 'folio': ticket.folio})
        messages.success(request, f'Recibimos tu solicitud. Folio {ticket.folio}. Fecha límite de respuesta: {timezone.localtime(ticket.due_at):%d/%m/%Y}.')
        return redirect('core:portal_support')
    return render(request, 'core/portal_support.html', {'form': form, 'tickets': Ticket.objects.filter(customer=customer)[:50]})

def portal_privacy(request):
    from django import forms
    from compliance.models import ARCORequest, DocumentVersion
    from compliance.services import create_arco_request
    customer = _portal_customer(request)
    class PrivacyForm(forms.Form):
        request_type = forms.ChoiceField(label='Derecho que deseas ejercer', choices=ARCORequest._meta.get_field('request_type').choices)
        description = forms.CharField(label='Describe tu solicitud', max_length=5000, widget=forms.Textarea(attrs={'rows': 4}))
    form = PrivacyForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        item = create_arco_request(customer, **form.cleaned_data, actor=request.user)
        messages.success(request, f'Solicitud ARCO-{item.pk} recibida. Verificaremos tu identidad antes de entregar o modificar datos personales.')
        return redirect('core:portal_privacy')
    return render(request, 'core/portal_privacy.html', {'form': form, 'requests': ARCORequest.objects.filter(customer=customer).order_by('-pk'),
        'documents': DocumentVersion.objects.filter(organization=customer.organization, status='approved', effective_on__lte=timezone.localdate()),
        'consents': customer.legal_consents.select_related('document').order_by('-accepted_at')})

def portal_consent(request, pk):
    from django import forms
    from compliance.models import DocumentVersion
    from compliance.services import record_consent
    customer = _portal_customer(request)
    document = get_object_or_404(DocumentVersion, pk=pk, organization=customer.organization, status='approved', effective_on__lte=timezone.localdate())
    class ConsentForm(forms.Form):
        confirm = forms.BooleanField(label='He leído y acepto este contrato' if document.kind == 'contract' else 'Confirmo la recepción de este documento')
    form = ConsentForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        purpose = {'contract': 'contract', 'privacy': 'privacy_notice'}.get(document.kind, 'document_delivery')
        record_consent(customer, document, purpose, 'portal', f'Confirmación expresa en sesión autenticada; usuario {request.user.pk}.', request.user)
        messages.success(request, 'Tu confirmación quedó registrada con esta versión del documento.')
        return redirect('core:portal_privacy')
    return render(request, 'core/portal_consent.html', {'document': document, 'form': form})
