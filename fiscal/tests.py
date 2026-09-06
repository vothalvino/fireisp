from decimal import Decimal
from unittest.mock import Mock,patch
import requests
from lxml import etree
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from core.models import Organization,Customer
from core.secrets import encrypt,decrypt
from billing.models import Invoice
from billing.services import receive_payment
from django.utils import timezone
from .models import FiscalDocument,FiscalProfile,GlobalItem
from . import services


class FiscalTests(TestCase):
    def setUp(self):
        self.staff=get_user_model().objects.create_user('admin',is_staff=True,is_superuser=True)
        self.owner=get_user_model().objects.create_user('owner')
        self.other=get_user_model().objects.create_user('other')
        self.organization=Organization.objects.create(name='ISP',demo_mode=True)
        self.customer=Customer.objects.create(organization=self.organization,code='C01',name='INMOBILIARIA CVA',rfc='ICV060329BY0',
            fiscal_regime='601',fiscal_postal_code='33826',invoice_use='G03',address='Test',user=self.owner)
        self.invoice=Invoice.objects.create(customer=self.customer,number='TEST',subtotal=100,tax=16,total=116)
        self.profile=FiscalProfile.objects.create(organization=self.organization,username_encrypted=encrypt('test-user'),password_encrypted=encrypt('test-secret'))

    def test_secrets_are_encrypted(self):
        self.assertNotIn('test-secret',self.profile.password_encrypted)
        self.assertEqual(decrypt(self.profile.password_encrypted),'test-secret')

    def test_production_is_never_implicitly_enabled(self):
        self.organization.demo_mode=False
        self.organization.save()
        with self.assertRaises(ValidationError):
            services._profile(self.organization)
        self.organization.demo_mode=True
        self.profile.environment='production'
        self.profile.save()
        with self.assertRaises(ValidationError):
            services._profile(self.organization)

    @patch('fiscal.services.soap_call')
    def test_verification_requires_emitter_record(self,soap):
        soap.return_value=etree.fromstring(b'<root xmlns:a="apps.services.soap.core.views"><a:message>RFC Invalido</a:message></root>')
        ok,_=services.verify_credentials(self.profile,self.staff)
        self.assertFalse(ok)
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.verified_at)
        soap.return_value=etree.fromstring(b'<root xmlns:a="apps.services.soap.core.views"><a:ResellerUser><a:taxpayer_id>EKU9003173C9</a:taxpayer_id></a:ResellerUser></root>')
        ok,_=services.verify_credentials(self.profile,self.staff)
        self.assertTrue(ok)

    def test_pue_rejects_unpaid_invoice_before_network(self):
        with self.assertRaisesMessage(ValidationError,'PUE requiere'):
            services.prepare_document(self.invoice,self.staff,method='PUE',payment_form='03')
        self.assertEqual(FiscalDocument.objects.count(),0)

    @patch('satcfdi.cfdi.CFDI.from_string',return_value=Mock())
    @patch('fiscal.services.pac')
    def test_timeout_requires_recovery_and_never_retries_stamp(self,pac,parse):
        document=FiscalDocument.objects.create(invoice=self.invoice,request_xml='<signed/>')
        pac.return_value.stamp.side_effect=requests.Timeout('network timeout')
        with self.assertRaises(ValidationError):
            services.stamp_document(document,self.staff)
        document.refresh_from_db()
        self.assertEqual(document.status,'uncertain')
        self.assertEqual(document.request_xml,'<signed/>')
        with self.assertRaises(ValidationError):
            services.stamp_document(document,self.staff)
        self.assertEqual(pac.return_value.stamp.call_count,1)

    @patch('satcfdi.cfdi.CFDI.from_string')
    @patch('fiscal.services.pac')
    def test_success_persists_xml_uuid_and_idempotent_result(self,pac,parse):
        document=FiscalDocument.objects.create(invoice=self.invoice,request_xml='<signed/>')
        parse.return_value={'Complemento':{'TimbreFiscalDigital':{'UUID':'00000000-0000-0000-0000-000000000001'}}}
        pac.return_value.stamp.return_value=Mock(document_id='00000000-0000-0000-0000-000000000001',xml=b'<stamped/>')
        result=services.stamp_document(document,self.staff)
        self.assertEqual(result.status,'stamped')
        self.assertEqual(result.xml,'<stamped/>')
        services.stamp_document(document,self.staff)
        self.assertEqual(pac.return_value.stamp.call_count,1)

    def test_xml_download_enforces_customer_ownership(self):
        document=FiscalDocument.objects.create(invoice=self.invoice,xml='<test/>',uuid='00000000-0000-0000-0000-000000000001',status='stamped')
        url=reverse('core:portal_document',args=[document.pk,'xml'])
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(url).status_code,404)
        self.client.force_login(self.owner)
        response=self.client.get(url)
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response['Cache-Control'])

    def test_error_redacts_provider_credentials(self):
        self.assertNotIn('test-secret',services._safe_error(Exception('bad test-secret'),self.profile))
        self.assertNotIn('<xml',services._safe_error(Exception('<xml password="secret"/>'),self.profile))

    @patch('satcfdi.cfdi.CFDI.from_string',return_value={'Total':Decimal('116.00'),'Receptor':{'Rfc':'ICV060329BY0'}})
    @patch('fiscal.services.soap_call')
    def test_cancellation_requires_confirmed_sat_state(self,soap,parse):
        document=FiscalDocument.objects.create(invoice=self.invoice,xml='<test/>',uuid='uuid',status='cancel_pending')
        soap.return_value=etree.fromstring(b'<root xmlns:a="apps.services.soap.core.views"><a:Estado>Vigente</a:Estado></root>')
        services.refresh_cancellation(document,self.staff)
        document.refresh_from_db()
        self.assertEqual(document.status,'cancel_pending')
        soap.return_value=etree.fromstring(b'<root xmlns:a="apps.services.soap.core.views"><a:Estado>Cancelado</a:Estado></root>')
        services.refresh_cancellation(document,self.staff)
        document.refresh_from_db()
        self.assertEqual(document.status,'cancelled')

    @patch('fiscal.services._build_cfdi')
    def test_global_reservation_blocks_individual_and_second_global(self,build):
        build.return_value.xml_bytes.return_value=b'<global/>'
        self.customer.rfc='XAXX010101000'
        self.customer.save()
        receive_payment(self.customer,'116.00','cash',self.staff,'global-payment')
        today=timezone.localdate()
        doc=services.prepare_global(self.organization,[self.invoice],today,today,'01','01','global1',self.staff)
        self.assertEqual(doc.kind,'global')
        self.assertIsNone(doc.invoice_id)
        self.assertEqual(GlobalItem.objects.count(),1)
        self.assertEqual(services.prepare_global(self.organization,[self.invoice],today,today,'01','01','global1',self.staff).pk,doc.pk)
        for periodicity,payment_form in [('02','01'),('01','03')]:
            with self.subTest(periodicity=periodicity,payment_form=payment_form),self.assertRaises(ValidationError):
                services.prepare_global(self.organization,[self.invoice],today,today,periodicity,payment_form,'global1',self.staff)
        with self.assertRaises(ValidationError):
            services.prepare_document(self.invoice,self.staff)
        with self.assertRaises(ValidationError):
            services.prepare_global(self.organization,[self.invoice],today,today,'01','01','global2',self.staff)

    @patch('fiscal.services._build_cfdi')
    def test_individual_reservation_blocks_global(self,build):
        build.return_value.xml_bytes.return_value=b'<income/>'
        self.customer.rfc='XAXX010101000'
        self.customer.save()
        receive_payment(self.customer,'116.00','cash',self.staff,'global-payment')
        services.prepare_document(self.invoice,self.staff)
        today=timezone.localdate()
        with self.assertRaises(ValidationError):
            services.prepare_global(self.organization,[self.invoice],today,today,'01','01','global1',self.staff)
