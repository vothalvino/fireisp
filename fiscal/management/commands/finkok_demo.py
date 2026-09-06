"""Explicit real DEMO integration. No credentials or certificate material in output."""
import json
import os
import stat
import uuid
from decimal import Decimal
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand,CommandError
from django.core.exceptions import ValidationError
from core.models import Customer,Organization
from core.secrets import encrypt
from billing.models import Invoice
from billing.services import receive_payment
from fiscal.models import FiscalDocument,FiscalProfile
from fiscal import services


class Command(BaseCommand):
    help='Importa credenciales privadas y ejecuta una prueba explícita en Finkok DEMO.'

    def add_arguments(self,parser):
        parser.add_argument('--credentials-file',help='JSON root:root 0600 con username y token o password.')
        parser.add_argument('--organization-id',type=int)
        parser.add_argument('--csd-zip')
        parser.add_argument('--fiel-zip')
        parser.add_argument('--verify',action='store_true')
        parser.add_argument('--issue-demo',action='store_true')
        parser.add_argument('--invoice-id',type=int)
        parser.add_argument('--document-id',type=int)
        parser.add_argument('--recover',action='store_true')
        parser.add_argument('--cancel',action='store_true')
        parser.add_argument('--check-cancel',action='store_true')

    def handle(self,*args,**options):
        organization=Organization.objects.filter(pk=options['organization_id']).first() if options['organization_id'] else Organization.objects.first()
        if not organization or not organization.demo_mode:
            raise CommandError('Se requiere una organización configurada en DEMO.')
        profile,_=FiscalProfile.objects.get_or_create(organization=organization)
        result={'environment':'demo','profile_id':profile.pk}
        if options['credentials_file']:
            try:
                fd=os.open(options['credentials_file'],os.O_RDONLY|os.O_NOFOLLOW)
                with os.fdopen(fd) as stream:
                    info=os.fstat(stream.fileno())
                    if info.st_uid not in {0,os.geteuid()} or stat.S_IMODE(info.st_mode)&0o077 or not stat.S_ISREG(info.st_mode):
                        raise CommandError('Las credenciales deben ser un archivo regular privado del usuario actual o de root, sin permisos de grupo ni de otros.')
                    if info.st_size>16384:
                        raise CommandError('Archivo de credenciales demasiado grande.')
                    data=json.load(stream)
                username=data.get('username')
                password=data.get('token') or data.get('password')
                if not username or not password:
                    raise CommandError('El JSON requiere username y token o password.')
                profile.username_encrypted=encrypt(username)
                profile.password_encrypted=encrypt(password)
                profile.save(update_fields=['username_encrypted','password_encrypted','updated_at'])
                result['credentials_imported']=True
            except (OSError,ValueError,TypeError):
                raise CommandError('No se pudo importar el archivo privado.') from None
        try:
            for name,fiel in [('csd_zip',False),('fiel_zip',True)]:
                if options[name]:
                    services.import_certificate_zip(profile,Path(options[name]).read_bytes(),'12345678a',fiel=fiel)
                    result[name+'_imported']=True
            actor=get_user_model().objects.filter(is_superuser=True,is_active=True).first()
            if options['verify']:
                ok,status=services.verify_credentials(profile,actor)
                result['connection_verified']=ok
                result['verification']=status
            document=FiscalDocument.objects.get(pk=options['document_id']) if options['document_id'] else None
            if document and services.document_organization(document).pk!=organization.pk:
                raise CommandError('Documento de otra organización.')
            if options['issue_demo']:
                if options['invoice_id']:
                    invoice=Invoice.objects.get(pk=options['invoice_id'],customer__organization=organization)
                else:
                    # Published Finkok receiver fixture; separate from actual customer data.
                    customer,_=Customer.objects.get_or_create(code='FINKOK-DEMO',defaults={'organization':organization,'name':'INMOBILIARIA CVA',
                        'address':'Receptor oficial de pruebas Finkok','rfc':'ICV060329BY0','fiscal_regime':'601','fiscal_postal_code':'33826','invoice_use':'G03'})
                    if customer.organization_id!=organization.pk:
                        raise CommandError('Código FINKOK-DEMO usado por otra organización.')
                    nonce=uuid.uuid4().hex[:16]
                    invoice=Invoice.objects.create(customer=customer,number='DEMO-'+nonce,description='Servicio de acceso a internet - prueba de integración',
                        subtotal=Decimal('100.00'),tax=Decimal('16.00'),total=Decimal('116.00'))
                    receive_payment(customer,Decimal('116.00'),'transfer',actor,'finkok-demo:'+nonce,'Prueba DEMO')
                document=services.prepare_document(invoice,actor,method='PUE',payment_form='03')
                document=services.stamp_document(document,actor)
            if options['recover']:
                if not document:
                    raise CommandError('--recover requiere --document-id.')
                document=services.stamp_document(document,actor,recover=True)
            if options['cancel']:
                if not document:
                    raise CommandError('--cancel requiere --document-id o --issue-demo.')
                document=services.cancel_document(document,actor,reason='02')
            if options['check_cancel']:
                if not document:
                    raise CommandError('--check-cancel requiere un documento.')
                result['sat_state']=services.refresh_cancellation(document,actor)
            if document:
                document.refresh_from_db()
                result.update(document_id=document.pk,invoice_id=document.invoice_id,uuid=document.uuid,status=document.status,
                    cancellation_code=document.cancellation_code,xml_saved=bool(document.xml),acknowledgement_saved=bool(document.cancellation_xml))
            self.stdout.write(json.dumps(result,ensure_ascii=False))
        except (ValidationError,FiscalDocument.DoesNotExist,Invoice.DoesNotExist) as exc:
            detail='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'No se encontró el documento solicitado.'
            if 'document' in locals() and document:
                detail+=f' Documento local: {document.pk}; mensualidad: {document.invoice_id}. Consulta el estado antes de volver a emitir.'
            raise CommandError(detail) from None
