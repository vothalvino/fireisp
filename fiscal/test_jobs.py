from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock, patch
import uuid
import requests
from lxml import etree
from django.core.exceptions import ValidationError
from django.db import connections, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from . import jobs, services, tests as fixtures
from .models import FiscalDocument, FiscalJob


class FiscalJobTests(TestCase):
    setUp = fixtures.FiscalTests.setUp

    def document(self, **kwargs):
        return FiscalDocument.objects.create(invoice=self.invoice, **kwargs)

    def test_duplicate_request_coalesces_and_conflicting_operation_waits(self):
        document = self.document(request_xml='<signed/>')
        first = jobs.queue_job('stamp', document=document, actor=self.staff, request_key='first')
        self.assertEqual(jobs.queue_job('stamp', document=document, actor=self.staff).pk, first.pk)
        self.assertEqual(jobs.queue_job('stamp', document=document, actor=self.staff, request_key='first').pk, first.pk)
        with self.assertRaises(ValidationError):
            jobs.queue_job('recover', document=document, actor=self.staff)
        with self.assertRaises(ValidationError):
            jobs.queue_job('recover', document=document, request_key='first')
        self.assertEqual(FiscalJob.objects.count(), 1)

    @patch('fiscal.tasks.process_fiscal_job.apply_async', side_effect=ConnectionError('broker down'))
    def test_broker_failure_keeps_committed_work_and_dispatcher_republishes(self, publish):
        with self.captureOnCommitCallbacks(execute=True):
            job = jobs.queue_job('verify', profile=self.profile, actor=self.staff)
        self.assertEqual(FiscalJob.objects.get(pk=job.pk).status, 'queued')
        self.assertEqual(publish.call_count, 1)
        with patch('fiscal.jobs._publish', return_value=True) as restored:
            self.assertEqual(jobs.dispatch_jobs(), 1)
        restored.assert_called_once_with(job.pk)

    @patch('fiscal.jobs._publish')
    def test_rollback_neither_persists_nor_publishes(self, publish):
        with self.captureOnCommitCallbacks(execute=True):
            with self.assertRaises(ValueError), transaction.atomic():
                jobs.queue_job('verify', profile=self.profile, actor=self.staff)
                raise ValueError('rollback')
        self.assertFalse(FiscalJob.objects.exists())
        publish.assert_not_called()

    @patch('fiscal.services._build_cfdi')
    @patch('fiscal.services.pac')
    def test_http_issue_only_reserves_and_queues_without_signing_or_pac(self, pac, build):
        self.client.force_login(self.staff)
        response = self.client.post(reverse('fiscal:invoice', args=[self.invoice.pk]), {'method': 'PPD', 'payment_form': '99'})
        self.assertEqual(response.status_code, 302)
        document = FiscalDocument.objects.get(invoice=self.invoice)
        self.assertEqual(document.request_xml, '')
        self.assertEqual(document.jobs.get().operation, 'stamp')
        build.assert_not_called()
        pac.assert_not_called()
        response = self.client.get(reverse('fiscal:invoice', args=[self.invoice.pk]))
        self.assertContains(response, 'Timbrado: Pendiente')

    @patch('fiscal.services.verify_credentials', return_value=(True, 'Conexión verificada.'))
    def test_verification_only_runs_after_worker_claim_and_duplicate_delivery_is_ignored(self, verify):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse('fiscal:verify')).status_code, 302)
        verify.assert_not_called()
        job = FiscalJob.objects.get()
        self.assertTrue(jobs.run_job(job.pk))
        self.assertFalse(jobs.run_job(job.pk))
        verify.assert_called_once()
        job.refresh_from_db()
        self.assertEqual(job.status, 'succeeded')
        self.assertEqual(job.attempts, 1)

    def test_pending_stamp_data_cannot_be_changed_by_second_request(self):
        document = services.prepare_document(self.invoice, self.staff, defer_build=True)
        jobs.queue_job('stamp', document=document)
        with self.assertRaises(ValidationError):
            services.prepare_document(self.invoice, self.staff, method='PUE', payment_form='03', defer_build=True)
        document.refresh_from_db()
        self.assertEqual(document.payment_method, 'PPD')

    @patch('fiscal.services.cancel_document')
    @patch('fiscal.services.refresh_cancellation')
    @patch('fiscal.services.stamp_document')
    def test_http_cancellation_status_and_recovery_do_not_call_provider(self, stamp, refresh, cancel):
        document = self.document(request_xml='<signed/>', xml='<stamped/>', status='stamped')
        self.client.force_login(self.staff)
        response = self.client.post(reverse('fiscal:cancel', args=[document.pk]), {'reason': '02'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(document.jobs.get().operation, 'cancel')
        document.jobs.update(status='succeeded')
        self.assertEqual(self.client.post(reverse('fiscal:cancellation_status', args=[document.pk])).status_code, 302)
        self.assertEqual(document.jobs.first().operation, 'cancellation_status')
        document.jobs.update(status='succeeded')
        document.status = 'uncertain'
        document.save(update_fields=['status'])
        self.assertEqual(self.client.post(reverse('fiscal:recover', args=[document.pk])).status_code, 302)
        self.assertEqual(document.jobs.first().operation, 'recover')
        stamp.assert_not_called()
        refresh.assert_not_called()
        cancel.assert_not_called()

    @patch('satcfdi.cfdi.CFDI.from_string', return_value=Mock())
    @patch('fiscal.services.pac')
    def test_transport_timeout_preserves_xml_and_requires_review_without_retrying_stamp(self, pac, parse):
        document = self.document(request_xml='<signed/>')
        pac.return_value.stamp.side_effect = requests.Timeout('test-secret provider response')
        job = jobs.queue_job('stamp', document=document)
        jobs.run_job(job.pk)
        job.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual((job.status, document.status), ('review', 'uncertain'))
        self.assertEqual(document.request_xml, '<signed/>')
        self.assertNotIn('test-secret', job.message)
        self.assertFalse(jobs.run_job(job.pk))
        with self.assertRaises(ValidationError):
            jobs.queue_job('stamp', document=document)
        self.assertEqual(pac.return_value.stamp.call_count, 1)

    @patch('fiscal.services.stamp_document')
    def test_interrupted_stamp_recovers_exact_xml_instead_of_second_stamp(self, stamp):
        document = self.document(request_xml='<exact-signed/>', status='submitting')
        job = FiscalJob.objects.create(document=document, operation='stamp', request_key='interrupted', status='running',
            attempts=1, lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(jobs.recover_expired_jobs(), 1)
        job.refresh_from_db()
        document.refresh_from_db()
        self.assertEqual(job.operation, 'recover')
        self.assertEqual(document.status, 'uncertain')
        jobs.run_job(job.pk)
        self.assertTrue(stamp.call_args.kwargs['recover'])
        self.assertEqual(stamp.call_args.args[0].request_xml, '<exact-signed/>')

    @patch('fiscal.services.refresh_cancellation', return_value='Vigente')
    @patch('fiscal.services.cancel_document')
    def test_interrupted_cancellation_only_checks_sat(self, cancel, refresh):
        document = self.document(xml='<stamped/>', status='cancel_pending')
        job = FiscalJob.objects.create(document=document, operation='cancel', request_key='cancel-interrupted',
            payload={'reason': '02', 'replacement': ''}, status='running', attempts=1,
            lease_until=timezone.now() - timedelta(seconds=1))
        jobs.recover_expired_jobs()
        jobs.run_job(job.pk)
        cancel.assert_not_called()
        refresh.assert_called_once()
        document.refresh_from_db()
        self.assertEqual(document.status, 'cancel_pending')

    def test_recovery_is_bounded_and_live_claims_are_not_stolen(self):
        job = jobs.queue_job('verify', profile=self.profile)
        claimed = jobs.claim_job(job.pk)
        self.assertIsNotNone(claimed)
        self.assertIsNone(jobs.claim_job(job.pk))
        self.assertEqual(jobs.recover_expired_jobs(), 0)
        FiscalJob.objects.filter(pk=job.pk).update(attempts=jobs.MAX_ATTEMPTS, lease_until=timezone.now()-timedelta(seconds=1))
        jobs.recover_expired_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, 'review')
        self.assertIsNone(jobs.claim_job(job.pk))

    @patch('satcfdi.render.pdf_bytes', return_value=b'%PDF-1.4\nexample')
    @patch('satcfdi.cfdi.CFDI.from_string', return_value=Mock())
    def test_pdf_runs_in_worker_then_downloads_durable_bytes_without_rendering(self, parse, render):
        document = self.document(xml='<stamped/>', status='stamped', uuid='00000000-0000-0000-0000-000000000001')
        url = reverse('core:portal_document', args=[document.pk, 'pdf'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(FiscalJob.objects.exists())
        self.client.force_login(self.owner)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response['Retry-After'], '10')
        self.assertIn('no-store', response['Cache-Control'])
        render.assert_not_called()
        job = FiscalJob.objects.get(document=document)
        self.assertEqual(job.operation, 'pdf')
        jobs.run_job(job.pk)
        render.assert_called_once()
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response.content, b'%PDF-1.4\nexample')
        render.assert_called_once()

    @patch('satcfdi.render.pdf_bytes', return_value=b'%PDF-' + b'x' * jobs.MAX_PDF_BYTES)
    @patch('satcfdi.cfdi.CFDI.from_string', return_value=Mock())
    def test_oversized_pdf_not_saved_and_stale_pdf_not_served(self, parse, render):
        document = self.document(xml='<stamped/>', status='stamped', pdf_content=b'%PDF-old', pdf_source_sha256='stale')
        self.assertFalse(jobs.pdf_ready(document))
        with self.assertRaises(ValidationError):
            jobs.render_pdf(document)
        document.refresh_from_db()
        self.assertEqual(bytes(document.pdf_content), b'%PDF-old')

    def test_xml_response_is_bounded_before_parse_and_always_closed(self):
        response = Mock()
        response.iter_content.return_value = [b'x' * services.MAX_XML_BYTES, b'x']
        with self.assertRaises(ValidationError):
            services._response_xml(response)
        response.close.assert_called_once()

    @patch('satcfdi.cfdi.CFDI.from_string', return_value={'Complemento': {'TimbreFiscalDigital': {'UUID': '00000000-0000-0000-0000-000000000001'}}})
    @patch('fiscal.services.pac')
    def test_stale_worker_success_and_error_cannot_overwrite_newer_document(self, pac, parse):
        document = self.document(request_xml='<signed/>')
        for fails in (False, True):
            with self.subTest(fails=fails):
                FiscalJob.objects.update(status='succeeded')
                FiscalDocument.objects.filter(pk=document.pk).update(status='draft')
                job = jobs.claim_job(jobs.queue_job('stamp', document=document).pk)
                def replaced_worker_result(*args):
                    FiscalJob.objects.filter(pk=job.pk).update(claim_token=uuid.uuid4())
                    FiscalDocument.objects.filter(pk=document.pk).update(status='cancelled', xml='<newer/>')
                    if fails:
                        raise requests.Timeout('late failure')
                    return Mock(document_id='00000000-0000-0000-0000-000000000001', xml=b'<old/>')
                pac.return_value.stamp.side_effect = replaced_worker_result
                with self.assertRaises(services.StaleFiscalClaim):
                    services.stamp_document(document, claim=job)
                document.refresh_from_db()
                self.assertEqual((document.status, document.xml), ('cancelled', '<newer/>'))
                self.assertFalse(document.attempts.exists())

    @patch('satcfdi.cfdi.CFDI.from_string', return_value=Mock())
    @patch('fiscal.services.load_signer', return_value=Mock())
    @patch('fiscal.services.pac')
    def test_stale_cancel_rejection_cannot_reset_confirmed_cancellation(self, pac, signer, parse):
        from satcfdi.exceptions import ResponseError
        document = self.document(xml='<stamped/>', status='stamped')
        job = jobs.claim_job(jobs.queue_job('cancel', document=document).pk)
        def rejected_after_replacement(*args, **kwargs):
            FiscalJob.objects.filter(pk=job.pk).update(claim_token=uuid.uuid4())
            FiscalDocument.objects.filter(pk=document.pk).update(status='cancelled')
            raise ResponseError('late rejection')
        pac.return_value.cancel.side_effect = rejected_after_replacement
        with self.assertRaises(services.StaleFiscalClaim):
            services.cancel_document(document, self.staff, claim=job)
        document.refresh_from_db()
        self.assertEqual(document.status, 'cancelled')

    @patch('fiscal.services.pac')
    def test_expired_claim_cannot_begin_an_external_mutation(self, pac):
        document = self.document(request_xml='<signed/>')
        job = jobs.claim_job(jobs.queue_job('stamp', document=document).pk)
        FiscalJob.objects.filter(pk=job.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(services.StaleFiscalClaim):
            services.stamp_document(document, claim=job)
        pac.assert_not_called()
        document.refresh_from_db()
        self.assertEqual(document.status, 'draft')

    @patch('fiscal.services.verify_credentials')
    def test_expired_worker_cannot_finalize_its_job_before_recovery(self, verify):
        job = jobs.queue_job('verify', profile=self.profile)
        def expired_result(*args, **kwargs):
            FiscalJob.objects.filter(pk=job.pk).update(lease_until=timezone.now()-timedelta(seconds=1))
            return True, 'late result'
        verify.side_effect = expired_result
        self.assertFalse(jobs.run_job(job.pk))
        job.refresh_from_db()
        self.assertEqual(job.status, 'running')
        self.assertEqual(jobs.recover_expired_jobs(), 1)

    @patch('fiscal.services.soap_call')
    def test_config_changed_during_verification_is_not_marked_verified(self, soap):
        def changed_configuration(*args, **kwargs):
            self.profile.issuer_rfc = 'AAA010101AAA'
            self.profile.save(update_fields=['issuer_rfc', 'updated_at'])
            return etree.fromstring(b'<root xmlns:a="apps.services.soap.core.views"><a:ResellerUser><a:taxpayer_id>EKU9003173C9</a:taxpayer_id></a:ResellerUser></root>')
        soap.side_effect = changed_configuration
        with self.assertRaises(services.StaleFiscalClaim):
            services.verify_credentials(self.profile)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.verified_at)


class FiscalJobConcurrencyTests(TransactionTestCase):
    setUp = fixtures.FiscalTests.setUp

    @skipUnlessDBFeature('has_select_for_update')
    def test_parallel_workers_only_one_claims_the_job(self):
        job = jobs.queue_job('verify', profile=self.profile)
        barrier = Barrier(2)
        def claim():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return jobs.claim_job(job.pk) is not None
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(sorted(result), [False, True])
        job.refresh_from_db()
        self.assertEqual(job.attempts, 1)

    @skipUnlessDBFeature('has_select_for_update')
    @patch('fiscal.jobs._publish')
    def test_parallel_enqueue_coalesces_to_one_durable_request(self, publish):
        barrier = Barrier(2)
        def enqueue():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return jobs.queue_job('verify', profile=self.profile).pk
            finally:
                connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: enqueue(), range(2)))
        self.assertEqual(result[0], result[1])
        self.assertEqual(FiscalJob.objects.count(), 1)
