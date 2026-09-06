"""Transactional prepaid ledger. Period end is exclusive; activation anchors anniversaries."""
import calendar
import csv
import hashlib
import io
from datetime import date, datetime,timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from core.models import Customer, Subscription
from core.services import audit,publish
from .models import Allocation, BankEntry, CashClosure, CashClosureItem, Invoice, Payment, PaymentReversal,CreditMemo,Refund,SuspensionPolicy,SuspensionProposal,SuspensionDecision,SuspensionApplication,SuspensionRelease


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal('.01')) or amount >= Decimal('10000000000'):
            raise ValueError()
        return amount
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError('El importe debe ser positivo y tener máximo dos decimales.')


def anniversary(start, months):
    year, month = divmod(start.year * 12 + start.month - 1 + months, 12)
    month += 1
    return date(year, month, min(start.day, calendar.monthrange(year, month)[1]))


def _actor_lock(actor):
    if actor and actor.pk:
        get_user_model().objects.select_for_update().get(pk=actor.pk)


@transaction.atomic
def create_period(subscription, start, end, actor=None):
    Customer.objects.select_for_update().get(pk=subscription.customer_id)
    subscription = Subscription.objects.select_for_update(of=("self",)).select_related('plan','customer').get(pk=subscription.pk)
    if not subscription.activated_at or subscription.status == 'pending':
        raise ValidationError('El periodo comienza al activar efectivamente el servicio.')
    activation = timezone.localtime(subscription.activated_at).date() if isinstance(subscription.activated_at, datetime) else subscription.activated_at
    index = (start.year - activation.year) * 12 + start.month - activation.month
    if index < 0 or start != anniversary(activation,index) or end != anniversary(activation,index+1):
        raise ValidationError('El periodo debe respetar el aniversario de activación.')
    existing = Invoice.objects.filter(subscription=subscription, period_start=start).first()
    if existing:
        return existing
    total = money(subscription.plan.price_mxn)
    subtotal = (total / (1 + subscription.plan.tax_rate)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
    invoice = Invoice.objects.create(customer=subscription.customer, subscription=subscription,
        number=f'M-{subscription.pk}-{start:%Y%m%d}', period_start=start, period_end=end,
        subtotal=subtotal,tax=total-subtotal,total=total,description=f'Internet {subscription.plan.name} · {start:%d/%m/%Y} al {end:%d/%m/%Y}')
    audit(actor, 'billing.invoice.created', invoice, {'total':str(total)})
    _allocate_customer(subscription.customer_id)
    return invoice


def activate_subscription(subscription, actor=None, activated_at=None):
    """Compatibility entry point; legal and network readiness belong to core."""
    if activated_at is not None:
        raise ValidationError('La activación registra la fecha real; no acepta fechas retroactivas.')
    from core.services import activate_subscription as activate
    return activate(subscription.pk,actor)


@transaction.atomic
def renew_subscription(subscription, actor=None):
    Customer.objects.select_for_update().get(pk=subscription.customer_id)
    subscription=Subscription.objects.select_for_update(of=("self",)).select_related('plan').get(pk=subscription.pk)
    if not subscription.activated_at or subscription.status=='cancelled':
        raise ValidationError('Se requiere una suscripción activada y no cancelada.')
    last=subscription.invoices.exclude(status='void').order_by('-period_start').first()
    if not last or last.balance>0:
        raise ValidationError('Liquida la mensualidad actual antes de renovar.')
    credit=sum((p.available for p in Payment.objects.filter(customer_id=subscription.customer_id,reversal__isnull=True)),Decimal(0))
    if credit<subscription.plan.price_mxn:
        raise ValidationError('Registra primero el abono completo para el siguiente mes. No se genera deuda automática.')
    activation=timezone.localtime(subscription.activated_at).date()
    start=last.period_end
    index=(start.year-activation.year)*12+start.month-activation.month
    return create_period(subscription,start,anniversary(activation,index+1),actor)


def _refresh_subscription(subscription_id):
    if not subscription_id:
        return
    subscription = Subscription.objects.select_for_update().get(pk=subscription_id)
    if not subscription.activated_at:
        return
    cursor = timezone.localtime(subscription.activated_at).date() if isinstance(subscription.activated_at, datetime) else subscription.activated_at
    for invoice in subscription.invoices.exclude(status='void').order_by('period_start'):
        if invoice.period_start != cursor or invoice.balance > 0:
            break
        cursor = invoice.period_end
    activation_local = timezone.localtime(subscription.activated_at)
    subscription.paid_until = timezone.make_aware(datetime.combine(cursor,activation_local.time()),activation_local.tzinfo)
    subscription.save(update_fields=['paid_until'])
    _resume_nonpayment(subscription)


def _allocate_customer(customer_id):
    """Caller holds customer lock; credits apply oldest charge first."""
    invoices = list(Invoice.objects.select_for_update().filter(customer_id=customer_id,status='open').order_by('period_start','created_at','pk'))
    payments = list(Payment.objects.filter(customer_id=customer_id,reversal__isnull=True).order_by('created_at','pk'))
    affected = set()
    for invoice in invoices:
        remaining = invoice.balance
        for payment in payments:
            available = payment.available
            applied = min(remaining, available)
            if applied > 0:
                Allocation.objects.create(payment=payment,invoice=invoice,amount=applied)
                remaining -= applied
            if remaining <= 0:
                break
        invoice.status = 'paid' if remaining <= 0 else 'open'
        invoice.save(update_fields=['status'])
        affected.add(invoice.subscription_id)
    for subscription_id in affected:
        _refresh_subscription(subscription_id)


@transaction.atomic
def receive_payment(customer, amount, method, actor, idempotency_key, reference='', paid_at=None):
    amount = money(amount)
    if method not in dict(Payment.METHODS) or not idempotency_key or len(idempotency_key)>100:
        raise ValidationError('Método o identificador de cobro inválido.')
    _actor_lock(actor)
    Customer.objects.select_for_update().get(pk=customer.pk)
    existing = Payment.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (existing.customer_id,existing.amount,existing.method,existing.reference) != (customer.pk,amount,method,reference):
            raise ValidationError('El identificador de cobro ya corresponde a otra operación.')
        return existing
    payment = Payment.objects.create(customer=customer,amount=amount,method=method,reference=reference,
        created_by=actor,paid_at=paid_at or timezone.now(),idempotency_key=idempotency_key)
    _allocate_customer(customer.pk)
    audit(actor,'billing.payment.received',payment,{'amount':str(amount),'method':method})
    return payment


@transaction.atomic
def reverse_payment(payment, actor, reason):
    if not reason.strip():
        raise ValidationError('Indica el motivo de la reversión.')
    _actor_lock(actor)
    Customer.objects.select_for_update().get(pk=payment.customer_id)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    existing = PaymentReversal.objects.filter(payment=payment).first()
    if existing:
        return existing
    if Refund.objects.filter(credit_memo__invoice__allocations__payment=payment).exists():
        raise ValidationError('Existe una devolución asociada a este cobro. Revisa los ajustes antes de reversarlo para evitar un doble reembolso.')
    reversal = PaymentReversal.objects.create(payment=payment,created_by=actor,reason=reason.strip())
    subscription_ids = set()
    for allocation in payment.allocations.select_related('invoice'):
        invoice = allocation.invoice
        if invoice.status != 'void':
            invoice.status = 'open'
            invoice.save(update_fields=['status'])
        subscription_ids.add(invoice.subscription_id)
    _allocate_customer(payment.customer_id)
    for subscription_id in subscription_ids:
        _refresh_subscription(subscription_id)
    audit(actor,'billing.payment.reversed',payment,{'reason':reason})
    return reversal


@transaction.atomic
def close_cash(actor, counted, notes=''):
    _actor_lock(actor)
    counted = Decimal(str(counted))
    if not counted.is_finite() or counted < 0 or counted != counted.quantize(Decimal('.01')):
        raise ValidationError('Conteo inválido.')
    payments = list(Payment.objects.filter(created_by=actor,method='cash',closure_item__isnull=True))
    reversals = list(PaymentReversal.objects.filter(created_by=actor,payment__method='cash',closure_item__isnull=True).select_related('payment'))
    refunds=list(Refund.objects.filter(created_by=actor,method='cash',closure_item__isnull=True))
    if not payments and not reversals and not refunds:
        raise ValidationError('No hay movimientos pendientes de corte.')
    expected = sum((p.amount for p in payments),Decimal(0)) - sum((r.payment.amount for r in reversals),Decimal(0)) - sum((r.amount for r in refunds),Decimal(0))
    closure = CashClosure.objects.create(cashier=actor,expected=expected,counted=counted,difference=counted-expected,notes=notes)
    CashClosureItem.objects.bulk_create([CashClosureItem(closure=closure,payment=p) for p in payments] + [CashClosureItem(closure=closure,reversal=r) for r in reversals] + [CashClosureItem(closure=closure,refund=r) for r in refunds])
    audit(actor,'billing.cash.closed',closure,{'expected':str(expected),'counted':str(counted)})
    return closure


@transaction.atomic
def import_bank_csv(organization, account, contents, actor):
    if not account.strip() or len(contents)>2_000_000:
        raise ValidationError('Cuenta requerida. Archivo máximo: 2 MB.')
    try:
        reader = csv.DictReader(io.StringIO(contents.decode('utf-8-sig')))
        if not {'external_reference','date','amount'}.issubset(reader.fieldnames or []):
            raise ValidationError('Columnas requeridas: external_reference,date,amount; opcionales: customer_code,description.')
        count = 0
        for index,row in enumerate(reader,2):
            if index > 10002:
                raise ValidationError('Máximo 10,000 movimientos por archivo.')
            reference = row['external_reference'].strip()
            if not reference or len(reference)>160:
                raise ValidationError(f'Referencia inválida en fila {index}.')
            amount = money(row['amount'])
            day = date.fromisoformat(row['date'])
            fingerprint = hashlib.sha256(f'{organization.pk}|{account.strip()}|{reference}'.encode()).hexdigest()
            entry,created = BankEntry.objects.get_or_create(fingerprint=fingerprint,defaults={'organization':organization,'account':account.strip(),
                'external_reference':reference,'date':day,'amount':amount,'customer_code':row.get('customer_code','').strip(),
                'description':row.get('description','')[:500]})
            if not created and (entry.amount != amount or entry.date != day):
                raise ValidationError(f'La referencia {reference} existe con un importe o fecha diferentes.')
            count += created
    except (UnicodeDecodeError, ValueError, KeyError, csv.Error):
        raise ValidationError('CSV inválido. Usa UTF-8, fecha AAAA-MM-DD e importes sin separadores de miles.')
    audit(actor,'billing.bank.imported',organization,{'rows':count,'account':account})
    return count


@transaction.atomic
def reconcile_bank(entry, customer, actor):
    _actor_lock(actor)
    Customer.objects.select_for_update().get(pk=customer.pk)
    entry = BankEntry.objects.select_for_update().get(pk=entry.pk)
    if entry.organization_id != customer.organization_id:
        raise ValidationError('El cliente pertenece a otra organización.')
    if entry.payment_id:
        if entry.payment.customer_id != customer.pk:
            raise ValidationError('El movimiento ya está conciliado con otro cliente.')
        return entry.payment
    payment = receive_payment(customer,entry.amount,'transfer',actor,f'bank:{entry.fingerprint}',entry.external_reference,
        timezone.make_aware(datetime.combine(entry.date,datetime.min.time())))
    entry.payment = payment
    entry.save(update_fields=['payment'])
    audit(actor,'billing.bank.reconciled',entry,{'payment_id':payment.pk})
    return payment


@transaction.atomic
def issue_credit_memo(invoice,amount,reason,source_key,actor):
    amount=money(amount)
    if not reason.strip() or not source_key:
        raise ValidationError('Indica motivo y referencia única del ajuste.')
    Customer.objects.select_for_update().get(pk=invoice.customer_id)
    invoice=Invoice.objects.select_for_update().get(pk=invoice.pk)
    existing=CreditMemo.objects.filter(source_key=source_key).first()
    if existing:
        if existing.invoice_id!=invoice.pk or existing.amount!=amount:
            raise ValidationError('La referencia de ajuste corresponde a otra operación.')
        return existing
    if invoice.status=='void':
        raise ValidationError('No puede ajustarse una mensualidad anulada.')
    memo=CreditMemo.objects.create(invoice=invoice,amount=amount,reason=reason,source_key=source_key,created_by=actor)
    invoice.status='paid' if invoice.balance<=0 else 'open'
    invoice.save(update_fields=['status'])
    _refresh_subscription(invoice.subscription_id)
    audit(actor,'billing.credit.applied',memo.pk,{'invoice_id':invoice.pk,'amount':str(amount),'source':source_key})
    return memo


@transaction.atomic
def apply_outage_credit(outage_credit,actor=None):
    from operations.models import OutageCredit
    # Same lock order as all customer ledger changes.
    customer_id=outage_credit.subscription.customer_id
    Customer.objects.select_for_update().get(pk=customer_id)
    source=OutageCredit.objects.select_for_update().get(pk=outage_credit.pk)
    start=timezone.localtime(source.period_start).date()
    invoice=Invoice.objects.filter(subscription_id=source.subscription_id,period_start=start).first()
    if not invoice:
        raise ValidationError('No existe una mensualidad para el periodo de la bonificación.')
    memo=issue_credit_memo(invoice,source.total,f'Bonificación por interrupción #{source.outage_id}',f'outage:{source.pk}',actor)
    source.applied_at=source.applied_at or timezone.now()
    source.ledger_reference=f'credit-memo:{memo.pk}'
    source.save(update_fields=['applied_at','ledger_reference'])
    return memo


@transaction.atomic
def refund_credit(memo,amount,method,reference,idempotency_key,actor):
    amount=money(amount)
    if method not in dict(Payment.METHODS) or not reference.strip() or not idempotency_key:
        raise ValidationError('Indica forma, referencia de devolución e identificador único.')
    _actor_lock(actor)
    Customer.objects.select_for_update().get(pk=memo.invoice.customer_id)
    memo=CreditMemo.objects.select_for_update(of=("self",)).select_related('invoice').get(pk=memo.pk)
    existing=Refund.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if (existing.credit_memo_id,existing.amount,existing.method,existing.reference)!=(memo.pk,amount,method,reference):
            raise ValidationError('La referencia de devolución ya corresponde a otra operación.')
        return existing
    already=memo.refunds.aggregate(total=Sum('amount'))['total'] or Decimal(0)
    if amount>memo.amount-already or amount>max(-memo.invoice.balance,Decimal(0)):
        raise ValidationError('La devolución excede el saldo a favor efectivamente cobrado.')
    refund=Refund.objects.create(credit_memo=memo,amount=amount,method=method,reference=reference,idempotency_key=idempotency_key,created_by=actor)
    audit(actor,'billing.refund.recorded',refund.pk,{'amount':str(amount),'method':method})
    return refund


def suspension_block(subscription,policy,now=None,check_health=True):
    from operations.services import has_billing_dispute_hold
    from operations.models import Outage
    from core.models import HealthCheck
    now=now or timezone.now()
    if subscription.status!='active' or not subscription.activated_at:
        return 'La suscripción no está activa o no se ha instalado.'
    if not subscription.paid_until or subscription.paid_until+timedelta(hours=policy.grace_hours)>now:
        return 'La vigencia no venció o permanece dentro del plazo de gracia.'
    if has_billing_dispute_hold(subscription):
        return 'Existe una aclaración de facturación abierta; suspensión bloqueada.'
    if Outage.objects.filter(subscriptions=subscription,ended_at__isnull=True).exists():
        return 'Existe una interrupción de servicio abierta; suspensión bloqueada.'
    if check_health:
        from network.models import RadiusCredential
        credential = RadiusCredential.objects.select_related('router__network_node').filter(subscription=subscription).first()
        health_code = credential.router.network_node.health_code if credential else 'network_sync'
        health=HealthCheck.objects.filter(code=health_code).first()
        if not health or health.status!='ok' or health.checked_at<now-timedelta(seconds=120):
            return 'Sin sincronización de red confirmada en los últimos 120 segundos.'
    return ''


@transaction.atomic
def propose_suspension(subscription,actor,idempotency_key):
    if not idempotency_key or len(idempotency_key)>100:
        raise ValidationError('Identificador de propuesta inválido.')
    Customer.objects.select_for_update().get(pk=subscription.customer_id)
    subscription=Subscription.objects.select_for_update(of=("self",)).select_related('customer').get(pk=subscription.pk)
    policy,_=SuspensionPolicy.objects.get_or_create(organization=subscription.customer.organization)
    existing=SuspensionProposal.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.subscription_id!=subscription.pk:
            raise ValidationError('La propuesta corresponde a otra suscripción.')
        return existing
    blocked=suspension_block(subscription,policy,check_health=False)
    if blocked:
        raise ValidationError(blocked)
    proposal=SuspensionProposal.objects.create(subscription=subscription,snapshot_paid_until=subscription.paid_until,created_by=actor,idempotency_key=idempotency_key)
    audit(actor,'billing.suspension.proposed',proposal.pk,{'subscription_id':subscription.pk,'paid_until':subscription.paid_until,'reason':'nonpayment'})
    return proposal


@transaction.atomic
def review_suspension(proposal,approved,note,actor):
    proposal=SuspensionProposal.objects.select_for_update().get(pk=proposal.pk)
    if not note.strip():
        raise ValidationError('Registra la justificación de la revisión.')
    existing=SuspensionDecision.objects.filter(proposal=proposal).first()
    if existing:
        if existing.approved!=approved:
            raise ValidationError('La propuesta ya fue revisada; genera una nueva para cambiar la decisión.')
        return existing
    decision=SuspensionDecision.objects.create(proposal=proposal,approved=approved,note=note,created_by=actor)
    audit(actor,'billing.suspension.reviewed',proposal.pk,{'approved':approved,'note':note})
    return decision


@transaction.atomic
def apply_suspension(proposal,actor=None):
    Customer.objects.select_for_update().get(pk=proposal.subscription.customer_id)
    subscription=Subscription.objects.select_for_update(of=("self",)).select_related('customer').get(pk=proposal.subscription_id)
    proposal=SuspensionProposal.objects.select_for_update().get(pk=proposal.pk)
    existing=SuspensionApplication.objects.filter(proposal=proposal).first()
    if existing:
        return existing
    decision=SuspensionDecision.objects.filter(proposal=proposal).first()
    if not decision or not decision.approved:
        raise ValidationError('La propuesta requiere una revisión aprobada antes de aplicarse.')
    policy,_=SuspensionPolicy.objects.get_or_create(organization=subscription.customer.organization)
    blocked=suspension_block(subscription,policy)
    if actor is None and not policy.automatic_enabled:
        blocked='La política automática fue deshabilitada antes de aplicar.'
    if subscription.paid_until!=proposal.snapshot_paid_until:
        blocked='La vigencia cambió después de la propuesta; se requiere una nueva revisión.'
    previous=subscription.status
    if not blocked:
        subscription.status='suspended'
        subscription.save(update_fields=['status'])
    application=SuspensionApplication.objects.create(proposal=proposal,applied=not blocked,previous_status=previous,
        resulting_status=subscription.status,detail=blocked or 'Suspensión por falta de pago aprobada; vigencia vencida y red sincronizada.',created_by=actor)
    audit(actor,'billing.suspension.applied' if not blocked else 'billing.suspension.blocked',application.pk,
        {'subscription_id':subscription.pk,'previous':previous,'result':subscription.status,'detail':application.detail})
    if not blocked:
        publish(f'suspension:{application.pk}','subscription.changed',{'subscription_id':subscription.pk})
        publish(f'suspension-notice:{application.pk}','notification',{'customer_id':subscription.customer_id,'title':'Servicio suspendido por vigencia vencida',
            'body':'Tu vigencia venció y se aplicó la suspensión revisada. Registra el abono de renovación o solicita una aclaración de facturación.'})
    return application


def _resume_nonpayment(subscription,actor=None):
    if subscription.status!='suspended' or not subscription.paid_until or subscription.paid_until<=timezone.now():
        return
    application=SuspensionApplication.objects.filter(proposal__subscription=subscription,applied=True,release__isnull=True).order_by('-created_at').first()
    if not application:
        return
    SuspensionRelease.objects.create(application=application,created_by=actor)
    subscription.status='active'
    subscription.save(update_fields=['status'])
    audit(actor,'billing.suspension.released',application.pk,{'subscription_id':subscription.pk,'paid_until':subscription.paid_until,'reason':'paid_renewal'})
    publish(f'nonpayment-release:{application.pk}','subscription.changed',{'subscription_id':subscription.pk})
    publish(f'nonpayment-release-notice:{application.pk}','notification',{'customer_id':subscription.customer_id,'title':'Renovación registrada',
        'body':'Tu renovación liberó la suspensión por falta de pago. La red está recibiendo la actualización de tu vigencia.'})
