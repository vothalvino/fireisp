# Facturación DEMO y libro de cobranza

La instalación limita el conector a Finkok DEMO: tanto el perfil fiscal como la organización deben permanecer en demostración. No existe interruptor web que habilite producción. Los documentos de prueba no tienen validez fiscal.

## Configurar y probar

Un superusuario abre **Facturación → Configurar Finkok**, captura el usuario del token y el token como contraseña, y carga CSD/FIEL en ZIP. Usuario, token, certificados y llaves se cifran con Fernet (`ENCRYPTION_KEY`). No se vuelven a mostrar ni se incluyen en bitácoras. Respalda la clave fuera del repositorio junto con un procedimiento de recuperación: cambiarla sin recifrar hace ilegibles las credenciales.

La verificación usa exclusivamente `registration.get` sobre TLS, con `reseller_username`, `reseller_password` y el RFC emisor. Sólo se declara verificada si la respuesta contiene ese RFC. Un HTTP 200 o una respuesta a `datetime` no prueban autenticación.

Para la prueba real desde un servidor, crea un JSON `root:root 0600` con claves `username` y `token`. No lo pases como argumento ni lo pegues en el historial. Con el entorno Django de despliegue cargado:

```bash
python manage.py finkok_demo --credentials-file /etc/fireisp/finkok-demo.json --csd-zip /etc/fireisp/demo-csd.zip --fiel-zip /etc/fireisp/demo-fiel.zip --verify
python manage.py finkok_demo --issue-demo --cancel --check-cancel
python manage.py finkok_demo --document-id 1 --check-cancel
```

El primer comando importa secretos y certificados; el segundo **crea una factura y cobro de prueba y solicita su cancelación real en DEMO**. Guarda los IDs que imprime. Repetir `--issue-demo` sin `--invoice-id` crea otra factura de prueba deliberadamente. Usa `--document-id ID --recover` para consultar un XML después de una respuesta ambigua. La salida contiene únicamente IDs, UUID y estados.

El fixture de integración usa el CSD público EKU9003173C9 (ESCUELA KEMPER URGATE, CP 20928) y el receptor ICV060329BY0 publicado por Finkok. Los ZIP se importan sin extraer rutas al disco. La contraseña `12345678a` corresponde únicamente a estos certificados públicos DEMO. El RFC emisor debe estar registrado previamente en la cuenta de Finkok; el comando no modifica el registro de emisores.

Fuentes oficiales consultadas:

- [Certificados y receptores de pruebas Finkok](https://wiki.finkok.com/home/certificados)
- [CSD público EKU](https://wiki.finkok.com/certificados/csd_eku9003173c9_20230517223903.zip)
- [FIEL pública EKU](https://wiki.finkok.com/fiel/fiel_eku9003173c9_20230517223532.zip)
- [Uso de token](https://wiki.finkok.com/en/home/token)
- [Método de consulta de emisor](https://wiki.finkok.com/home/webservices/registro_de_clientes/get)
- [WSDL DEMO](https://demo-facturacion.finkok.com/servicios/soap/registration.wsdl)
- [Biblioteca SAT-CFDI, código primario](https://github.com/SAT-CFDI/python-satcfdi)

## Estados y recuperación

El importe de una mensualidad y el estado de su CFDI son independientes. PUE exige saldo cero y una forma de pago conocida. PPD usa forma 99; los abonos aplicados pueden generar CFDI de pago con complemento 2.0, uno por aplicación, respetando el orden de parcialidades. Reversar un cobro no cancela automáticamente los CFDI asociados: requiere revisión y solicitud fiscal explícita.

El XML firmado se guarda antes de llamar al PAC. Un timeout conserva el estado **Por recuperar**: nunca se reintenta automáticamente un timbrado potencialmente realizado. La recuperación consulta el mismo XML firmado mediante `stamped`. Las cancelaciones se marcan **pendientes** al solicitarse y sólo **canceladas** cuando `get_sat_status` informa `Cancelado`. El acuse queda guardado y descargable.

XML, PDF y acuses se entregan mediante vistas autenticadas; un cliente sólo accede a los documentos asociados a su usuario. Los secretos y XML no se publican en un directorio estático.

## Cobranza

Los cobros son asientos inmutables con identificador de idempotencia. Las correcciones son reversiones separadas y conservan las aplicaciones originales para auditoría. Los servicios bloquean al cliente en transacción para impedir sobreaplicaciones concurrentes; PostgreSQL es el motor de despliegue. Un saldo a favor se conserva hasta que exista una mensualidad.

La primera mensualidad sólo se crea después de la activación real y los periodos posteriores conservan ese aniversario. Un alta pendiente no inicia el reloj. Los periodos terminan de forma exclusiva; una activación el 31 de enero termina el 28/29 de febrero y la siguiente mensualidad termina el 31 de marzo. La vigencia sólo avanza a través de periodos consecutivos totalmente pagados.

Los cortes de caja incluyen cobros en efectivo y reversiones todavía no cerradas del cajero. Cada asiento entra en un solo corte. La conciliación bancaria importa CSV UTF-8 con `external_reference,date,amount` y opcionalmente `customer_code,description`; las fechas usan AAAA-MM-DD y los importes positivos sin separador de miles. La referencia es única por organización y cuenta. Importar no cobra: un operador confirma el cliente antes de registrar el cobro idempotente.

## Bonificaciones, devoluciones y factura global

`billing.services.apply_outage_credit(source, actor)` aplica la bonificación calculada en operaciones a una mensualidad existente y sólo entonces marca el registro de interrupción como aplicado. La nota de ajuste es inmutable, no crea un cobro ficticio y reduce `Invoice.balance`. Un saldo negativo es saldo a favor. Registrar una devolución exige saldo a favor realmente cobrado y un comprobante de salida; su importe no puede superar la nota ni el saldo. Las devoluciones en efectivo entran en el corte de caja y bloquean una reversión posterior que duplicaría el reembolso.

Los CFDI de egreso se emiten desde **Bonificaciones y devoluciones**, relacionados con la factura original mediante relación 01. El egreso usa los datos del receptor del XML original. Los complementos posteriores descuentan los egresos timbrados previos al calcular el saldo anterior. Primero deben timbrarse las parcialidades y egresos anteriores; no se inventa una secuencia cuando falta un comprobante. Una factura no puede cancelarse mientras tenga complementos o egresos vigentes.

La factura global admite hasta 500 operaciones liquidadas de público en general (`XAXX010101000`) por lote, con periodicidad diaria, semanal, quincenal o mensual y fechas dentro del mismo mes. Cada ticket queda reservado mediante una relación única. Los servicios rechazan tanto incluir una factura individual en una global como facturar individualmente una operación reservada. Las reservas se conservan después de cancelar una global para evitar una doble emisión accidental; cualquier sustitución exige una revisión fiscal explícita. Una global no genera un segundo cargo en el libro de cobranza.

## Vencimiento, revisión y reconexión

**Cobranza → Revisar suspensiones** muestra vigencias vencidas y permite crear una propuesta, registrar aprobación/rechazo y aplicar en un paso separado. Propuesta, revisión, aplicación y liberación son registros inmutables (también mediante triggers en PostgreSQL). Aplicar comprueba nuevamente que la vigencia no cambió, que expiró la gracia, que la suscripción sigue activa y que no existen aclaraciones de facturación ni interrupciones abiertas.

La suspensión también exige `core.HealthCheck(code='network_sync', status='ok')` con menos de 120 segundos de antigüedad, emitido por el trabajador de red tras una sincronización confirmada. Si falta, está obsoleto o informa error, no se suspende. El proceso web publica `subscription.changed` en el outbox; no llama a sockets privilegiados.

La automatización está desactivada de inicio. Sólo un superusuario puede habilitarla y cambiar el plazo de gracia (24 horas de inicio). `billing.tasks.evaluate_suspensions` usa la misma propuesta, revisión auditable y verificaciones finales; `billing.tasks.renewal_preview` sólo genera avisos, sin crear deuda. Una renovación pagada libera únicamente una suspensión originada en este flujo por falta de pago; una suspensión ajena a este flujo no se reactiva automáticamente.

## Evidencia de integración del 5 de septiembre de 2026

Se verificaron contra Finkok DEMO: consulta autenticada de emisor, CFDI PUE, PPD, complemento 2.0 parcial, egreso de $11.60, segundo complemento de $46.40 con saldo final $0.00 y global de dos operaciones. Se generó PDF a partir del XML timbrado. La cancelación mediante la API firmada de SAT-CFDI devolvió acuse y código 201; la consulta posterior devolvió `No Encontrado`, por lo que permanece pendiente de confirmación. Código 201 no se presenta como cancelación final; [Finkok documenta expresamente esta distinción](https://wiki.finkok.com/home/webservices/ws_cancelacion).
