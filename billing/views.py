from django.contrib import messages
from django.core.exceptions import ValidationError,PermissionDenied
import uuid
from django.db.models import Sum
from django.shortcuts import get_object_or_404,redirect,render
from core.models import Organization,Subscription
from core.security import staff_required
from .forms import BankImportForm,CashClosureForm,PaymentForm,ReconcileForm,ReversalForm,CreditMemoForm,RefundForm,SuspensionPolicyForm,SuspensionReviewForm
from .models import BankEntry,CashClosure,Invoice,Payment,CreditMemo,Refund,SuspensionPolicy,SuspensionProposal
from . import services


@staff_required
def index(request):
    return render(request,'billing/index.html',{'invoices':Invoice.objects.with_balances().select_related('customer')[:100],
        'payments':Payment.objects.select_related('customer','created_by','reversal')[:50],
        'pending_bank':BankEntry.objects.filter(payment__isnull=True).count(),
        'collected':(Payment.objects.filter(reversal__isnull=True).aggregate(total=Sum('amount'))['total'] or 0)-(Refund.objects.aggregate(total=Sum('amount'))['total'] or 0),
        'subscriptions':Subscription.objects.exclude(activated_at=None).exclude(status='cancelled').select_related('customer','plan')[:100]})


@staff_required
def renew(request,pk):
    subscription=get_object_or_404(Subscription,pk=pk)
    if request.method=='POST':
        try:
            invoice=services.renew_subscription(subscription,request.user)
            messages.success(request,f'Mes renovado: {invoice.number}.')
        except ValidationError as exc:
            messages.error(request,'; '.join(exc.messages))
        return redirect('billing:index')
    return render(request,'billing/renew.html',{'subscription':subscription})


@staff_required
def payment_create(request):
    form = PaymentForm(request.POST or None,initial={'customer':request.GET.get('customer')})
    if request.method=='POST' and form.is_valid():
        try:
            payment = services.receive_payment(actor=request.user,**form.cleaned_data)
            messages.success(request,f'Cobro #{payment.pk} registrado. El saldo y la vigencia fueron actualizados.')
            return redirect('billing:receipt',pk=payment.pk)
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'form.html',{'form':form,'title':'Registrar cobro'})


@staff_required
def receipt(request,pk):
    payment = get_object_or_404(Payment.objects.select_related('customer','created_by'),pk=pk)
    return render(request,'billing/receipt.html',{'payment':payment})


@staff_required
def payment_reverse(request,pk):
    payment = get_object_or_404(Payment,pk=pk)
    form = ReversalForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            services.reverse_payment(payment,request.user,form.cleaned_data['reason'])
            messages.success(request,'Reversión registrada. El cobro original permanece en el historial.')
            return redirect('billing:receipt',pk=pk)
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'form.html',{'form':form,'title':f'Reversar cobro #{payment.pk}'})


@staff_required
def cash(request):
    form = CashClosureForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            closure = services.close_cash(request.user,**form.cleaned_data)
            messages.success(request,f'Corte #{closure.pk}: diferencia ${closure.difference}.')
            return redirect('billing:cash')
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'billing/cash.html',{'form':form,'closures':CashClosure.objects.select_related('cashier').order_by('-closed_at')[:50]})


@staff_required
def bank(request):
    form = BankImportForm(request.POST or None,request.FILES or None)
    if request.method=='POST' and form.is_valid():
        try:
            organization = Organization.objects.first()
            count = services.import_bank_csv(organization,form.cleaned_data['account'],form.cleaned_data['file'].read(2_000_001),request.user)
            messages.success(request,f'{count} movimientos nuevos importados. Selecciona el cliente y confirma cada conciliación.')
            return redirect('billing:bank')
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'billing/bank.html',{'form':form,'entries':BankEntry.objects.select_related('payment')[:200]})


@staff_required
def reconcile(request,pk):
    entry = get_object_or_404(BankEntry,pk=pk)
    form = ReconcileForm(request.POST or None)
    form.fields['customer'].queryset = form.fields['customer'].queryset.filter(organization=entry.organization)
    if request.method=='POST' and form.is_valid():
        try:
            services.reconcile_bank(entry,form.cleaned_data['customer'],request.user)
            messages.success(request,'Depósito conciliado y cobro registrado.')
            return redirect('billing:bank')
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'form.html',{'form':form,'title':f'Conciliar ${entry.amount} · {entry.external_reference} · sugerencia {entry.customer_code or "sin referencia de cliente"}'})


@staff_required
def credits(request):
    form=CreditMemoForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            services.issue_credit_memo(actor=request.user,**form.cleaned_data)
            messages.success(request,'Nota de ajuste aplicada al saldo. El CFDI de egreso se emite por separado.')
            return redirect('billing:credits')
        except ValidationError as exc:
            form.add_error(None,exc)
    from operations.models import OutageCredit
    return render(request,'billing/credits.html',{'form':form,'memos':CreditMemo.objects.select_related('invoice__customer').order_by('-created_at')[:100],
        'outage_credits':OutageCredit.objects.filter(applied_at__isnull=True).select_related('subscription__customer')[:100],
        'refunds':Refund.objects.select_related('credit_memo__invoice__customer').order_by('-created_at')[:100]})


@staff_required
def apply_outage(request,pk):
    if request.method!='POST':
        return redirect('billing:credits')
    from operations.models import OutageCredit
    source=get_object_or_404(OutageCredit,pk=pk)
    try:
        services.apply_outage_credit(source,request.user)
        messages.success(request,'Bonificación por interrupción aplicada al libro de cobranza.')
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
    return redirect('billing:credits')


@staff_required
def refund(request,pk):
    memo=get_object_or_404(CreditMemo,pk=pk)
    form=RefundForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            services.refund_credit(memo,actor=request.user,**form.cleaned_data)
            messages.success(request,'Devolución registrada. El efectivo se incluirá en tu próximo corte.')
            return redirect('billing:credits')
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'form.html',{'form':form,'title':f'Registrar devolución del ajuste #{memo.pk}'})


@staff_required
def suspensions(request):
    organization=Organization.objects.first()
    policy,_=SuspensionPolicy.objects.get_or_create(organization=organization)
    form=SuspensionPolicyForm(request.POST or None,instance=policy)
    if request.method=='POST':
        if not request.user.is_superuser:
            raise PermissionDenied
        if form.is_valid():
            from core.services import audit
            form.save()
            audit(request.user,'billing.suspension.policy.updated',organization.pk,{'automatic_enabled':policy.automatic_enabled,'grace_hours':policy.grace_hours})
            messages.success(request,'Política de suspensión guardada.')
            return redirect('billing:suspensions')
    from django.utils import timezone
    candidates=[]
    for subscription in Subscription.objects.filter(customer__organization=organization,status='active',paid_until__lte=timezone.now()).select_related('customer','plan')[:200]:
        candidates.append({'subscription':subscription,'block':services.suspension_block(subscription,policy)})
    return render(request,'billing/suspensions.html',{'form':form,'policy':policy,'candidates':candidates,'proposal_token':uuid.uuid4().hex,
        'proposals':SuspensionProposal.objects.filter(subscription__customer__organization=organization).select_related('subscription__customer','decision','application').order_by('-created_at')[:100]})


@staff_required
def suspension_propose(request,pk):
    if request.method!='POST':
        return redirect('billing:suspensions')
    subscription=get_object_or_404(Subscription,pk=pk)
    try:
        proposal=services.propose_suspension(subscription,request.user,request.POST.get('key',''))
        return redirect('billing:suspension_review',pk=proposal.pk)
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
        return redirect('billing:suspensions')


@staff_required
def suspension_review(request,pk):
    proposal=get_object_or_404(SuspensionProposal,pk=pk)
    form=SuspensionReviewForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        try:
            services.review_suspension(proposal,actor=request.user,**form.cleaned_data)
            messages.success(request,'Revisión registrada. Aplicar vuelve a verificar todas las condiciones vigentes.')
            return redirect('billing:suspensions')
        except ValidationError as exc:
            form.add_error(None,exc)
    return render(request,'form.html',{'form':form,'title':f'Revisar suspensión #{proposal.pk}: {proposal.subscription.customer} · vencimiento {proposal.snapshot_paid_until:%d/%m/%Y %H:%M}'})


@staff_required
def suspension_apply(request,pk):
    if request.method!='POST':
        return redirect('billing:suspensions')
    proposal=get_object_or_404(SuspensionProposal,pk=pk)
    try:
        application=services.apply_suspension(proposal,request.user)
        (messages.success if application.applied else messages.warning)(request,application.detail)
    except ValidationError as exc:
        messages.error(request,'; '.join(exc.messages))
    return redirect('billing:suspensions')
