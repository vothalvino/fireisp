"""CFDI 4.0 in Finkok DEMO only. Persist the signed payload before external effects."""
import base64
import io
import json
import zipfile
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import requests
from lxml import etree
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from core.secrets import encrypt,decrypt
from core.services import audit
from core.models import Customer
from billing.models import Invoice
from .models import FiscalDocument,FiscalProfile,FiscalAttempt,GlobalBatch,GlobalItem

DEMO_HOST = 'https://demo-facturacion.finkok.com'
SOAP = 'http://schemas.xmlsoap.org/soap/envelope/'
APP = 'apps.services.soap.core.views'
PAYMENT_FORMS = {'cash':'01','transfer':'03','card':'04'}


def document_organization(document):
    return document.global_batch.organization if document.kind=='global' else document.invoice.customer.organization


def _profile(organization):
    profile = FiscalProfile.objects.get(organization=organization)
    if profile.environment != 'demo' or not organization.demo_mode:
        raise ValidationError('Este conector sólo permite el ambiente DEMO. Producción requiere una habilitación independiente.')
    if not profile.username_encrypted or not profile.password_encrypted:
        raise ValidationError('Configura las credenciales de Finkok DEMO.')
    return profile


def _safe_error(exc,profile):
    message = str(exc)
    for encrypted in [profile.username_encrypted,profile.password_encrypted]:
        value = decrypt(encrypted)
        if value:
            message = message.replace(value,'[protegido]')
    # Never expose a server-supplied XML envelope, stack, request or credential field.
    if '<' in message or 'password' in message.lower() or 'token' in message.lower():
        return f'El PAC rechazó la operación ({type(exc).__name__}). Revisa la configuración y vuelve a consultar el estado.'
    return message[:500]


def soap_call(profile,service,method,fields):
    if service not in {'registration','cancel'}:
        raise ValueError('Unsupported SOAP service')
    namespace = f'http://facturacion.finkok.com/{service}'
    root=etree.Element(etree.QName(SOAP,'Envelope'),nsmap={'soap':SOAP,'ns':namespace})
    body=etree.SubElement(root,etree.QName(SOAP,'Body'))
    operation=etree.SubElement(body,etree.QName(namespace,method))
    for name,value in fields.items():
        element=etree.SubElement(operation,etree.QName(namespace,name))
        if isinstance(value,etree._Element):
            element.append(value)
        else:
            element.text=str(value)
    response=requests.post(f'{DEMO_HOST}/servicios/soap/{service}',data=etree.tostring(root),
        headers={'Content-Type':'text/xml; charset=utf-8','SOAPAction':method},timeout=(10,45))
    response.raise_for_status()
    parser=etree.XMLParser(resolve_entities=False,no_network=True)
    result=etree.fromstring(response.content,parser)
    if result.find(f'.//{{{SOAP}}}Fault') is not None:
        raise ValidationError('El PAC devolvió una falla SOAP.')
    return result


def verify_credentials(profile,actor=None):
    profile=_profile(profile.organization)
    try:
        result=soap_call(profile,'registration','get',{'reseller_username':decrypt(profile.username_encrypted),
            'reseller_password':decrypt(profile.password_encrypted),'taxpayer_id':profile.issuer_rfc})
        users=result.findall(f'.//{{{APP}}}ResellerUser')
        matched=any(u.findtext(f'{{{APP}}}taxpayer_id')==profile.issuer_rfc for u in users)
        message=result.findtext(f'.//{{{APP}}}message') or ''
        if matched:
            status='Conexión verificada: emisor registrado en Finkok DEMO.'
            profile.verified_at=timezone.now()
        else:
            status='No fue posible verificar el emisor. '+_safe_error(ValidationError(message or 'RFC sin registro o sin acceso.'),profile)
            profile.verified_at=None
        profile.verification_status=status[:300]
        profile.save(update_fields=['verified_at','verification_status','updated_at'])
        audit(actor,'fiscal.connection.verified',profile.pk,{'verified':matched})
        return matched,status
    except Exception as exc:
        profile.verified_at=None
        profile.verification_status=_safe_error(exc,profile)[:300]
        profile.save(update_fields=['verified_at','verification_status','updated_at'])
        raise ValidationError(profile.verification_status) from None


def load_signer(profile,fiel=False):
    from satcfdi.models import Signer
    value=profile.fiel_encrypted if fiel else profile.csd_encrypted
    if not value:
        raise ValidationError('Carga el certificado DEMO y su llave privada.')
    data=json.loads(decrypt(value))
    signer=Signer.load(base64.b64decode(data['certificate']),base64.b64decode(data['key']),data['password'])
    if signer.rfc != profile.issuer_rfc:
        raise ValidationError('El certificado no corresponde al RFC emisor configurado.')
    return signer


def import_certificate_zip(profile,contents,password,fiel=False):
    if len(contents)>1_000_000:
        raise ValidationError('El archivo de certificados es demasiado grande.')
    try:
        archive=zipfile.ZipFile(io.BytesIO(contents))
        certificates=[n for n in archive.namelist() if n.lower().endswith('.cer')]
        keys=[n for n in archive.namelist() if n.lower().endswith('.key')]
        if len(certificates)!=1 or len(keys)!=1 or any(i.file_size>100000 for i in archive.infolist()):
            raise ValueError()
        data={'certificate':base64.b64encode(archive.read(certificates[0])).decode(),
              'key':base64.b64encode(archive.read(keys[0])).decode(),'password':password}
        from satcfdi.models import Signer
        signer=Signer.load(base64.b64decode(data['certificate']),base64.b64decode(data['key']),password)
        if signer.rfc != profile.issuer_rfc:
            raise ValidationError('RFC del certificado distinto al emisor configurado.')
    except ValidationError:
        raise
    except Exception:
        raise ValidationError('ZIP, certificado, llave o contraseña no válidos.') from None
    field='fiel_encrypted' if fiel else 'csd_encrypted'
    setattr(profile,field,encrypt(json.dumps(data)))
    profile.save(update_fields=[field,'updated_at'])


def pac(profile):
    from satcfdi.pacs.finkok import Finkok
    from satcfdi.pacs import Environment
    class BoundedFinkok(Finkok):
        def _perform_request(self,url,envelope):
            if not url.startswith(DEMO_HOST+'/servicios/soap/'):
                raise ValidationError('Destino fiscal fuera del ambiente DEMO.')
            response=requests.post(url,data=etree.tostring(envelope),headers={'Content-Type':'text/xml; charset=utf-8'},timeout=(10,60))
            response.raise_for_status()
            root=etree.fromstring(response.content,etree.XMLParser(resolve_entities=False,no_network=True))
            if root.find(f'.//{{{SOAP}}}Fault') is not None:
                raise ValidationError('El PAC devolvió una falla SOAP.')
            return root
    return BoundedFinkok(decrypt(profile.username_encrypted),decrypt(profile.password_encrypted),environment=Environment.TEST)


def _build_cfdi(document,profile):
    from satcfdi.create.cfd import cfdi40,pago20
    if document.kind=='global':
        batch=document.global_batch
        concepts=[]
        for item in batch.items.select_related('invoice'):
            invoice=item.invoice
            rate=(invoice.tax/invoice.subtotal).quantize(Decimal('.01')).quantize(Decimal('.000001')) if invoice.subtotal else Decimal('0.000000')
            concepts.append(cfdi40.Concepto(clave_prod_serv='01010101',no_identificacion=invoice.number,cantidad=1,clave_unidad='ACT',descripcion='Venta',
                valor_unitario=invoice.subtotal,objeto_imp='02',impuestos=cfdi40.Impuestos(traslados=[cfdi40.Traslado(impuesto='002',tipo_factor='Tasa',tasa_o_cuota=rate)])))
        cfdi=cfdi40.Comprobante(emisor=cfdi40.Emisor(rfc=profile.issuer_rfc,nombre=profile.issuer_name,regimen_fiscal=profile.fiscal_regime),
            receptor=cfdi40.Receptor(rfc='XAXX010101000',nombre='PUBLICO EN GENERAL',domicilio_fiscal_receptor=profile.postal_code,regimen_fiscal_receptor='616',uso_cfdi='S01'),
            lugar_expedicion=profile.postal_code,metodo_pago='PUE',forma_pago=document.payment_form,serie='DEMO-G',folio=str(document.local_id.int)[:20],
            informacion_global=cfdi40.InformacionGlobal(periodicidad=batch.periodicity,meses=f'{batch.period_start.month:02d}',ano=batch.period_start.year),conceptos=concepts,
            fecha=datetime.now(ZoneInfo('America/Mexico_City')).replace(tzinfo=None))
        cfdi.sign(load_signer(profile))
        return cfdi.process()
    customer=document.invoice.customer
    if document.kind=='income':
        for value in [customer.rfc,customer.name,customer.fiscal_regime,customer.fiscal_postal_code,customer.invoice_use]:
            if not value:
                raise ValidationError('Completa RFC, razón social, régimen, código postal y uso CFDI del cliente.')
        receptor=cfdi40.Receptor(rfc=customer.rfc,nombre=customer.name,domicilio_fiscal_receptor=customer.fiscal_postal_code,
            regimen_fiscal_receptor=customer.fiscal_regime,uso_cfdi=customer.invoice_use)
    else:
        from satcfdi.cfdi import CFDI
        parent=FiscalDocument.objects.filter(invoice=document.invoice,kind='income',status='stamped').first()
        if not parent and document.kind=='credit' and hasattr(document.invoice,'global_item'):
            parent=FiscalDocument.objects.filter(global_batch=document.invoice.global_item.batch,status='stamped').first()
        if not parent:
            raise ValidationError('Se requiere la factura original timbrada y vigente.')
        original=CFDI.from_string(parent.xml.encode())
        original_receiver=original['Receptor']
        if original['Emisor']['Rfc']!=profile.issuer_rfc:
            raise ValidationError('El emisor configurado no corresponde a la factura original.')
        receptor=cfdi40.Receptor(rfc=original_receiver['Rfc'],nombre=original_receiver['Nombre'],domicilio_fiscal_receptor=original_receiver['DomicilioFiscalReceptor'],
            regimen_fiscal_receptor=original_receiver['RegimenFiscalReceptor'],uso_cfdi='CP01' if document.kind=='payment' else original_receiver['UsoCFDI'])
    emisor=cfdi40.Emisor(rfc=profile.issuer_rfc,nombre=profile.issuer_name,regimen_fiscal=profile.fiscal_regime)
    args={'emisor':emisor,'receptor':receptor,'lugar_expedicion':profile.postal_code,'serie':'DEMO',
        'folio':str(document.local_id.int)[:20],'fecha':datetime.now(ZoneInfo('America/Mexico_City')).replace(tzinfo=None)}
    invoice=document.invoice
    rate=(invoice.tax/invoice.subtotal).quantize(Decimal('.01')).quantize(Decimal('.000001')) if invoice.subtotal else Decimal('0.000000')
    if document.kind=='income':
        if document.payment_method=='PUE' and invoice.paid_amount<invoice.total:
            raise ValidationError('PUE requiere que la mensualidad esté totalmente pagada.')
        args.update(metodo_pago=document.payment_method,forma_pago=document.payment_form if document.payment_method=='PUE' else '99',
            conceptos=[cfdi40.Concepto(clave_prod_serv='81112100',cantidad=1,clave_unidad='E48',descripcion=invoice.description,
                valor_unitario=invoice.subtotal,objeto_imp='02',impuestos=cfdi40.Impuestos(traslados=[cfdi40.Traslado(impuesto='002',tipo_factor='Tasa',tasa_o_cuota=rate)]))])
    elif document.kind=='credit':
        memo=document.credit_memo
        parent=FiscalDocument.objects.filter(invoice=invoice,kind='income',status='stamped').first()
        if not parent and hasattr(invoice,'global_item'):
            parent=FiscalDocument.objects.filter(global_batch=invoice.global_item.batch,status='stamped').first()
        if not parent:
            raise ValidationError('Timbrar la factura original antes de emitir su CFDI de egreso.')
        base=(memo.amount/(1+rate)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        args.update(tipo_de_comprobante='E',metodo_pago='PUE',forma_pago='15',cfdi_relacionados=cfdi40.CfdiRelacionados(tipo_relacion='01',cfdi_relacionado=[parent.uuid]),
            conceptos=[cfdi40.Concepto(clave_prod_serv='84111506',cantidad=1,clave_unidad='ACT',descripcion=memo.reason,valor_unitario=base,objeto_imp='02',
                impuestos=cfdi40.Impuestos(traslados=[cfdi40.Traslado(impuesto='002',tipo_factor='Tasa',tasa_o_cuota=rate)]))])
    else:
        allocation=document.allocation
        if hasattr(allocation.payment,'reversal'):
            raise ValidationError('No puede timbrarse un cobro reversado.')
        parent=FiscalDocument.objects.get(invoice=invoice,kind='income',status='stamped',payment_method='PPD')
        previous=list(invoice.allocations.filter(pk__lt=allocation.pk,payment__reversal__isnull=True).order_by('pk'))
        if any(not FiscalDocument.objects.filter(allocation=a,status='stamped').exists() for a in previous):
            raise ValidationError('Timbrar primero los complementos de las parcialidades anteriores.')
        previous_total=sum((a.amount for a in previous),Decimal(0))
        prior_credits=list(invoice.credit_memos.filter(created_at__lte=allocation.created_at))
        if any(not FiscalDocument.objects.filter(credit_memo=m,status='stamped').exists() for m in prior_credits):
            raise ValidationError('Timbrar los CFDI de egreso pendientes antes de este complemento de pago.')
        credit_total=sum((m.amount for m in prior_credits),Decimal(0))
        previous_balance=invoice.total-previous_total-credit_total
        if allocation.amount>previous_balance:
            raise ValidationError('La aplicación excede el saldo fiscal del comprobante relacionado.')
        base=(allocation.amount/(1+rate)).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        related=pago20.DoctoRelacionado(id_documento=parent.uuid,moneda_dr='MXN',num_parcialidad=len(previous)+1,
            imp_saldo_ant=previous_balance,imp_pagado=allocation.amount,objeto_imp_dr='02',equivalencia_dr=1,
            impuestos_dr=pago20.ImpuestosDR(traslados_dr=[pago20.TrasladoDR(base_dr=base,impuesto_dr='002',tipo_factor_dr='Tasa',tasa_o_cuota_dr=rate,importe_dr=allocation.amount-base)]))
        payment=pago20.Pago(fecha_pago=timezone.localtime(allocation.payment.paid_at).replace(tzinfo=None),forma_de_pago_p=PAYMENT_FORMS[allocation.payment.method],
            moneda_p='MXN',tipo_cambio_p=1,monto=allocation.amount,docto_relacionado=[related])
        args.update(tipo_de_comprobante='P',moneda='XXX',conceptos=[cfdi40.Concepto(clave_prod_serv='84111506',cantidad=1,clave_unidad='ACT',descripcion='Pago',valor_unitario=0,objeto_imp='01')],
            complemento=pago20.Pagos(pago=[payment]))
    cfdi=cfdi40.Comprobante(**args)
    cfdi.sign(load_signer(profile))
    return cfdi.process()


@transaction.atomic
def prepare_document(invoice,actor=None,method='PPD',payment_form='99',allocation=None):
    invoice=Invoice.objects.select_for_update(of=("self",)).select_related('customer__organization').get(pk=invoice.pk)
    if invoice.status=='void':
        raise ValidationError('No se puede facturar una mensualidad anulada.')
    profile=_profile(invoice.customer.organization)
    if not allocation and GlobalItem.objects.filter(invoice=invoice).exists():
        raise ValidationError('Esta mensualidad ya está reservada en una factura global; no puede facturarse individualmente.')
    if method not in {'PUE','PPD'} or payment_form not in {'01','03','04','28','99'}:
        raise ValidationError('Método o forma de pago inválidos.')
    if method=='PUE' and payment_form=='99':
        raise ValidationError('PUE requiere una forma de pago conocida.')
    if allocation:
        if allocation.invoice_id != invoice.pk:
            raise ValidationError('Aplicación ajena a la mensualidad.')
        document,_=FiscalDocument.objects.get_or_create(allocation=allocation,defaults={'invoice':invoice,'kind':'payment','payment_method':'PPD'})
    else:
        document,_=FiscalDocument.objects.get_or_create(invoice=invoice,kind='income',defaults={'payment_method':method,'payment_form':payment_form})
    if not document.request_xml or document.status=='error':
        document.payment_method=method if not allocation else 'PPD'
        document.payment_form=payment_form
        document.request_xml=_build_cfdi(document,profile).xml_bytes().decode()
        document.status='draft'
        document.error=''
        document.save(update_fields=['payment_method','payment_form','request_xml','status','error'])
        if not allocation:
            invoice.fiscal_method=method
            invoice.save(update_fields=['fiscal_method'])
        audit(actor,'fiscal.document.prepared',document.pk,{'kind':document.kind,'environment':'demo'})
    return document


def stamp_document(document,actor=None,recover=False):
    from satcfdi.cfdi import CFDI
    from satcfdi.exceptions import ResponseError
    with transaction.atomic():
        document=FiscalDocument.objects.select_for_update(of=('self',)).select_related('invoice__customer__organization').get(pk=document.pk)
        if document.status in {'stamped','cancelled','cancel_pending'}:
            return document
        if document.status in {'submitting','uncertain'} and not recover:
            raise ValidationError('La operación puede estar timbrada. Recupera el mismo XML antes de reenviar.')
        if not document.request_xml:
            raise ValidationError('Prepara el documento antes de timbrarlo.')
        profile=_profile(document_organization(document))
        document.status='submitting'
        document.error=''
        document.save(update_fields=['status','error'])
    try:
        client=pac(profile)
        cfdi=CFDI.from_string(document.request_xml.encode())
        result=client.stamped(cfdi) if recover else client.stamp(cfdi)
        returned=CFDI.from_string(result.xml)
        stamped_uuid=returned['Complemento']['TimbreFiscalDigital']['UUID']
        if str(stamped_uuid).upper()!=str(result.document_id).upper():
            raise ValidationError('UUID inconsistente en respuesta del PAC.')
        with transaction.atomic():
            document=FiscalDocument.objects.select_for_update().get(pk=document.pk)
            document.xml=result.xml.decode()
            document.uuid=str(stamped_uuid)
            document.status='stamped'
            document.stamped_at=timezone.now()
            document.error=''
            document.save(update_fields=['xml','uuid','status','stamped_at','error'])
            audit(actor,'fiscal.document.stamped',document.pk,{'uuid':document.uuid,'environment':'demo','recovered':recover})
        return document
    except Exception as exc:
        # Transport ambiguity never triggers an automatic second stamp.
        status='error' if isinstance(exc,ResponseError) and not recover else 'uncertain'
        FiscalAttempt.objects.create(document=document,request_xml=document.request_xml,outcome=status,error=_safe_error(exc,profile))
        FiscalDocument.objects.filter(pk=document.pk).update(status=status,error=_safe_error(exc,profile))
        raise ValidationError(_safe_error(exc,profile)) from None


def cancel_document(document,actor,reason='02',replacement=''):
    if reason not in {'01','02','03','04'} or (reason=='01' and not replacement):
        raise ValidationError('Motivo inválido; el motivo 01 requiere UUID sustituto.')
    with transaction.atomic():
        document=FiscalDocument.objects.select_for_update(of=('self',)).select_related('invoice__customer__organization').get(pk=document.pk)
        if document.status in {'cancelled','cancel_pending'}:
            return document
        if document.status!='stamped':
            raise ValidationError('Sólo puede cancelarse un documento timbrado.')
        if document.kind in {'income','global'}:
            related_invoice_ids=[document.invoice_id] if document.invoice_id else list(document.global_batch.items.values_list('invoice_id',flat=True))
            if FiscalDocument.objects.filter(invoice_id__in=related_invoice_ids,kind__in=['credit','payment'],status__in=['stamped','submitting','uncertain','cancel_pending']).exists():
                raise ValidationError('Cancela y confirma primero los complementos y egresos relacionados.')
        profile=_profile(document_organization(document))
        signer=load_signer(profile,fiel=bool(profile.fiel_encrypted))
        document.status='cancel_pending'
        document.cancellation_reason=reason
        document.cancellation_replacement=replacement
        document.save(update_fields=['status','cancellation_reason','cancellation_replacement'])
    try:
        from satcfdi.cfdi import CFDI
        from satcfdi.pacs import CancelReason
        acknowledgement=pac(profile).cancel(CFDI.from_string(document.xml.encode()),CancelReason(reason),replacement or None,signer=signer)
        document.cancellation_xml=(acknowledgement.acuse or b'').decode()
        document.cancellation_code=str(acknowledgement.code or '')
        document.error=''
        document.save(update_fields=['cancellation_xml','cancellation_code','error'])
        audit(actor,'fiscal.cancellation.requested',document.pk,{'uuid':document.uuid,'code':document.cancellation_code,'reason':reason})
        return document
    except Exception as exc:
        from satcfdi.exceptions import ResponseError
        updates={'error':_safe_error(exc,profile)}
        if isinstance(exc,ResponseError):
            updates['status']='stamped'
        FiscalDocument.objects.filter(pk=document.pk).update(**updates)
        raise ValidationError(_safe_error(exc,profile)) from None


def refresh_cancellation(document,actor=None):
    from satcfdi.cfdi import CFDI
    profile=_profile(document_organization(document))
    cfdi=CFDI.from_string(document.xml.encode())
    result=soap_call(profile,'cancel','get_sat_status',{'username':decrypt(profile.username_encrypted),'password':decrypt(profile.password_encrypted),
        'taxpayer_id':profile.issuer_rfc,'rtaxpayer_id':cfdi['Receptor']['Rfc'],'uuid':document.uuid,'total':str(cfdi['Total'])})
    state=result.findtext(f'.//{{{APP}}}Estado') or ''
    if state.lower()=='cancelado':
        document.status='cancelled'
        document.error=''
        document.save(update_fields=['status','error'])
        audit(actor,'fiscal.cancellation.confirmed',document.pk,{'uuid':document.uuid})
    return state or 'Sin estado confirmado'


@transaction.atomic
def prepare_credit_document(memo,actor=None):
    Customer.objects.select_for_update().get(pk=memo.invoice.customer_id)
    invoice=Invoice.objects.select_for_update().get(pk=memo.invoice_id)
    profile=_profile(invoice.customer.organization)
    document,_=FiscalDocument.objects.get_or_create(credit_memo=memo,defaults={'invoice':invoice,'kind':'credit','payment_method':'PUE','payment_form':'15'})
    if not document.request_xml or document.status=='error':
        document.request_xml=_build_cfdi(document,profile).xml_bytes().decode()
        document.status='draft'
        document.error=''
        document.save(update_fields=['request_xml','status','error'])
        audit(actor,'fiscal.credit.prepared',document.pk,{'credit_memo_id':memo.pk})
    return document


@transaction.atomic
def prepare_global(organization,invoices,period_start,period_end,periodicity,payment_form,idempotency_key,actor=None):
    ids=sorted({int(i.pk if hasattr(i,'pk') else i) for i in invoices})
    if not ids or len(ids)>500 or period_start>period_end or period_start.month!=period_end.month or period_start.year!=period_end.year:
        raise ValidationError('Selecciona entre 1 y 500 operaciones del mismo mes y un periodo válido.')
    if periodicity not in {'01','02','03','04'} or payment_form not in {'01','03','04','28'} or not idempotency_key:
        raise ValidationError('Periodicidad, forma de pago o identificador inválidos.')
    def previous_result():
        existing=GlobalBatch.objects.select_related('document').filter(idempotency_key=idempotency_key).first()
        if existing:
            if (existing.organization_id,existing.period_start,existing.period_end,existing.periodicity,existing.document.payment_form)!=(organization.pk,period_start,period_end,periodicity,payment_form) or sorted(existing.items.values_list('invoice_id',flat=True))!=ids:
                raise ValidationError('La solicitud global ya corresponde a otras operaciones o datos fiscales.')
            return existing.document
        return None
    existing=previous_result()
    if existing:
        return existing
    customer_ids=Invoice.objects.filter(pk__in=ids).values_list('customer_id',flat=True)
    list(Customer.objects.select_for_update().filter(pk__in=customer_ids).order_by('pk'))
    selected=list(Invoice.objects.select_for_update(of=('self',)).filter(pk__in=ids).select_related('customer').order_by('pk'))
    # A concurrent identical request may have completed while these locks waited.
    existing=previous_result()
    if existing:
        return existing
    if len(selected)!=len(ids):
        raise ValidationError('No se encontraron todas las mensualidades seleccionadas.')
    for invoice in selected:
        if invoice.customer.organization_id!=organization.pk or invoice.customer.rfc!='XAXX010101000' or invoice.paid_amount<invoice.total or invoice.status=='void':
            raise ValidationError('La factura global requiere operaciones liquidadas de público en general (RFC XAXX010101000).')
        if not period_start<=timezone.localtime(invoice.created_at).date()<=period_end:
            raise ValidationError('La fecha de una operación está fuera del periodo seleccionado.')
        if invoice.fiscal_documents.filter(kind='income').exists() or GlobalItem.objects.filter(invoice=invoice).exists():
            raise ValidationError('Una operación ya tiene factura individual o está reservada en otra global.')
    profile=_profile(organization)
    batch=GlobalBatch.objects.create(organization=organization,period_start=period_start,period_end=period_end,periodicity=periodicity,idempotency_key=idempotency_key)
    GlobalItem.objects.bulk_create([GlobalItem(batch=batch,invoice=invoice) for invoice in selected])
    document=FiscalDocument.objects.create(global_batch=batch,kind='global',payment_method='PUE',payment_form=payment_form)
    document.request_xml=_build_cfdi(document,profile).xml_bytes().decode()
    document.save(update_fields=['request_xml'])
    audit(actor,'fiscal.global.prepared',document.pk,{'invoice_ids':ids,'count':len(ids),'environment':'demo'})
    return document
