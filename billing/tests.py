from datetime import datetime,timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone
from core.models import Organization,Customer,Plan,Subscription
from .models import Allocation,BankEntry,Invoice,Payment,PaymentReversal,CashClosureItem
from .services import anniversary,create_period,receive_payment,reverse_payment,close_cash,import_bank_csv,reconcile_bank,renew_subscription,issue_credit_memo,refund_credit
from .services import propose_suspension,review_suspension,apply_suspension
from .models import SuspensionPolicy,SuspensionProposal,SuspensionRelease


class LedgerTests(TestCase):
    def setUp(self):
        self.actor=get_user_model().objects.create_user('cashier',is_staff=True)
        self.organization=Organization.objects.create(name='Test ISP')
        self.customer=Customer.objects.create(organization=self.organization,code='C01',name='Cliente',address='Domicilio')
        self.plan=Plan.objects.create(organization=self.organization,name='Fibra 50',download_mbps=50,upload_mbps=20,price_mxn=Decimal('116.00'))
        self.start=datetime(2026,1,31,12,tzinfo=ZoneInfo('America/Chihuahua'))
        self.subscription=Subscription.objects.create(customer=self.customer,plan=self.plan,access_username='test',activated_at=self.start,status='active')

    def period(self):
        return create_period(self.subscription,self.start.date(),anniversary(self.start.date(),1),self.actor)

    def pay(self,amount,key='key',method='cash'):
        return receive_payment(self.customer,Decimal(amount),method,self.actor,key)

    def test_pending_installation_cannot_create_charge(self):
        self.subscription.activated_at=None
        self.subscription.status='pending'
        self.subscription.save()
        with self.assertRaises(ValidationError):
            self.period()
        self.assertEqual(Invoice.objects.count(),0)

    def test_month_end_uses_original_anniversary(self):
        invoice=self.period()
        self.assertEqual(str(invoice.period_end),'2026-02-28')
        self.assertEqual(invoice.subtotal,Decimal('100.00'))
        self.pay('232.00')
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.paid_until.date(),invoice.period_end)
        renewed=renew_subscription(self.subscription,self.actor)
        self.assertEqual(str(renewed.period_end),'2026-03-31')
        self.assertEqual(renewed.balance,Decimal('0.00'))
        self.assertEqual(Invoice.objects.count(),2)

    def test_partial_payment_does_not_grant_full_period(self):
        invoice=self.period()
        self.pay('58.00','first')
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.paid_until,self.start)
        self.pay('58.00','second')
        self.subscription.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status,'paid')
        self.assertEqual(self.subscription.paid_until.date(),invoice.period_end)

    def test_idempotency_preserves_single_payment_and_rejects_conflict(self):
        self.period()
        first=self.pay('116.00')
        self.assertEqual(self.pay('116.00').pk,first.pk)
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(Allocation.objects.count(),1)
        with self.assertRaises(ValidationError):
            self.pay('115.00')

    def test_duplicate_period_reuses_original_price(self):
        invoice=self.period()
        self.plan.price_mxn=Decimal('232.00')
        self.plan.save()
        self.assertEqual(self.period().pk,invoice.pk)
        self.assertEqual(self.period().total,Decimal('116.00'))

    def test_prepayment_is_credit_until_real_period_exists(self):
        payment=self.pay('200.00')
        self.assertEqual(payment.available,Decimal('200.00'))
        invoice=self.period()
        self.assertEqual(invoice.balance,Decimal(0))
        self.assertEqual(payment.available,Decimal('84.00'))

    def test_reversal_preserves_entries_and_reduces_vigency(self):
        invoice=self.period()
        payment=self.pay('116.00')
        reversal=reverse_payment(payment,self.actor,'Captura incorrecta')
        self.assertEqual(reverse_payment(payment,self.actor,'Segundo intento').pk,reversal.pk)
        invoice.refresh_from_db()
        self.subscription.refresh_from_db()
        self.assertEqual(invoice.balance,Decimal('116.00'))
        self.assertEqual(invoice.status,'open')
        self.assertEqual(Allocation.objects.count(),1)
        self.assertEqual(PaymentReversal.objects.count(),1)
        self.assertEqual(self.subscription.paid_until,self.start)

    def test_entry_updates_and_deletes_are_rejected(self):
        payment=self.pay('1.00')
        for operation in [lambda:payment.save(),lambda:payment.delete(),lambda:Payment.objects.filter(pk=payment.pk).update(amount=2),lambda:Payment.objects.filter(pk=payment.pk).delete()]:
            with self.assertRaises(ValidationError):
                operation()

    def test_invalid_precision_is_not_silently_rounded(self):
        for value in ['0','-1','1.001','NaN','Infinity']:
            with self.assertRaises(ValidationError):
                self.pay(value,value)

    def test_cash_closure_cannot_count_same_entry_twice(self):
        payment=self.pay('116.00')
        closure=close_cash(self.actor,'115.00','Falta un peso')
        self.assertEqual(closure.difference,Decimal('-1.00'))
        self.assertEqual(CashClosureItem.objects.count(),1)
        with self.assertRaises(ValidationError):
            close_cash(self.actor,'115.00')
        reverse_payment(payment,self.actor,'Devolución posterior al corte')
        next_close=close_cash(self.actor,'0.00')
        self.assertEqual(next_close.expected,Decimal('-116.00'))
        self.assertEqual(CashClosureItem.objects.count(),2)

    def test_bank_import_reconciliation_is_idempotent(self):
        self.period()
        csv=b'external_reference,date,amount,customer_code\nBANK-1,2026-01-31,116.00,C01\n'
        self.assertEqual(import_bank_csv(self.organization,'principal',csv,self.actor),1)
        self.assertEqual(import_bank_csv(self.organization,'principal',csv,self.actor),0)
        entry=BankEntry.objects.get()
        payment=reconcile_bank(entry,self.customer,self.actor)
        self.assertEqual(reconcile_bank(entry,self.customer,self.actor).pk,payment.pk)
        self.assertEqual(Payment.objects.count(),1)
        self.assertEqual(payment.method,'transfer')

    def test_csv_error_rolls_back_entire_import(self):
        csv=b'external_reference,date,amount\nGOOD,2026-01-31,100.00\nBAD,no-date,1.00\n'
        with self.assertRaises(ValidationError):
            import_bank_csv(self.organization,'principal',csv,self.actor)
        self.assertEqual(BankEntry.objects.count(),0)

    def test_renewal_requires_credit_and_does_not_create_debt(self):
        self.period()
        self.pay('116.00')
        with self.assertRaises(ValidationError):
            renew_subscription(self.subscription,self.actor)
        self.assertEqual(Invoice.objects.count(),1)

    def test_credit_is_adjustment_not_fake_cash(self):
        invoice=self.period()
        memo=issue_credit_memo(invoice,'20.00','Interrupción','credit-1',self.actor)
        self.assertEqual(invoice.balance,Decimal('96.00'))
        self.assertEqual(Payment.objects.count(),0)
        self.assertEqual(issue_credit_memo(invoice,'20.00','Interrupción','credit-1',self.actor).pk,memo.pk)
        with self.assertRaises(ValidationError):
            refund_credit(memo,'1.00','cash','R1','refund1',self.actor)

    def test_bulk_balances_match_ledger_without_per_invoice_queries(self):
        invoice=self.period()
        self.pay('50.00')
        issue_credit_memo(invoice,'20.00','Interrupción','credit-bulk',self.actor)
        with self.assertNumQueries(1):
            amounts=[(i.paid_amount,i.balance) for i in Invoice.objects.with_balances()]
        self.assertEqual(amounts,[(Decimal('50.00'),Decimal('46.00'))])

    def test_refund_is_bounded_and_enters_cash_closure(self):
        invoice=self.period()
        payment=self.pay('116.00')
        memo=issue_credit_memo(invoice,'20.00','Interrupción','credit-1',self.actor)
        refund=refund_credit(memo,'20.00','cash','R1','refund1',self.actor)
        self.assertEqual(refund_credit(memo,'20.00','cash','R1','refund1',self.actor).pk,refund.pk)
        self.assertEqual(invoice.balance,Decimal(0))
        closure=close_cash(self.actor,'96.00')
        self.assertEqual(closure.expected,Decimal('96.00'))
        with self.assertRaises(ValidationError):
            refund_credit(memo,'1.00','cash','R2','refund2',self.actor)
        with self.assertRaises(ValidationError):
            reverse_payment(payment,self.actor,'No debe permitir doble devolución')


class SuspensionTests(TestCase):
    pay=LedgerTests.pay

    def setUp(self):
        LedgerTests.setUp(self)
        from core.models import HealthCheck
        self.policy=SuspensionPolicy.objects.create(organization=self.organization,grace_hours=0)
        self.health=HealthCheck.objects.create(code='network_sync',status='ok')
        self.subscription.paid_until=timezone.now()-timedelta(days=3)
        self.subscription.save(update_fields=['paid_until'])

    def approved(self,key='proposal1'):
        proposal=propose_suspension(self.subscription,self.actor,key)
        review_suspension(proposal,True,'Vigencia revisada y notificación preparada.',self.actor)
        return proposal

    def test_apply_rechecks_payment_after_review(self):
        proposal=self.approved()
        self.subscription.paid_until=timezone.now()+timedelta(days=30)
        self.subscription.save(update_fields=['paid_until'])
        application=apply_suspension(proposal,self.actor)
        self.assertFalse(application.applied)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,'active')

    def test_apply_rechecks_new_billing_dispute(self):
        from operations.models import Ticket
        proposal=self.approved()
        Ticket.objects.create(customer=self.customer,kind='billing',subject='Aclaración',description='Pago por revisar')
        self.assertFalse(apply_suspension(proposal,self.actor).applied)

    def test_stale_network_blocks_apply(self):
        from core.models import HealthCheck
        proposal=self.approved()
        HealthCheck.objects.filter(pk=self.health.pk).update(checked_at=timezone.now()-timedelta(seconds=121))
        self.assertFalse(apply_suspension(proposal,self.actor).applied)

    def test_cancelled_subscription_is_not_suspended(self):
        proposal=self.approved()
        self.subscription.status='cancelled'
        self.subscription.save(update_fields=['status'])
        self.assertFalse(apply_suspension(proposal,self.actor).applied)
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,'cancelled')

    def test_unreviewed_proposal_cannot_apply(self):
        proposal=propose_suspension(self.subscription,self.actor,'pending')
        with self.assertRaises(ValidationError):
            apply_suspension(proposal,self.actor)

    def test_nonpayment_suspend_and_paid_resume_publish_state_changes(self):
        from core.models import OutboxEvent
        self.start=timezone.now()-timedelta(days=3)
        self.subscription.activated_at=self.start
        self.subscription.save(update_fields=['activated_at'])
        local_start=timezone.localtime(self.start).date()
        create_period(self.subscription,local_start,anniversary(local_start,1),self.actor)
        proposal=self.approved()
        application=apply_suspension(proposal,self.actor)
        self.assertTrue(application.applied)
        self.assertEqual(apply_suspension(proposal,self.actor).pk,application.pk)
        self.pay('116.00')
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,'active')
        self.assertEqual(SuspensionRelease.objects.count(),1)
        self.assertEqual(OutboxEvent.objects.filter(topic='subscription.changed').count(),2)

    def test_other_suspension_reason_is_not_auto_resumed(self):
        self.start=timezone.now()-timedelta(days=3)
        self.subscription.activated_at=self.start
        self.subscription.status='suspended'
        self.subscription.save(update_fields=['activated_at','status'])
        start=timezone.localtime(self.start).date()
        create_period(self.subscription,start,anniversary(start,1),self.actor)
        self.pay('116.00')
        self.subscription.refresh_from_db()
        self.assertEqual(self.subscription.status,'suspended')

    def test_automation_is_off_by_default(self):
        from .tasks import evaluate_suspensions
        self.assertFalse(self.policy.automatic_enabled)
        self.assertEqual(evaluate_suspensions(),0)
        self.assertEqual(SuspensionProposal.objects.count(),0)
