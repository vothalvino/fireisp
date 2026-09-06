# Aprovisionamiento de red

El módulo `network` conserva intención y resultados en PostgreSQL. La interfaz
`/network/` permite registrar un router, observar su huella SSH sin enviar
credenciales, confiar explícitamente en esa identidad, descubrir configuración
sin secretos, revisar un plan y aprobar su aplicación. Los cambios se ejecutan
mediante trabajos durables; se registra cada intención antes de modificar un
recurso. Reintentar usa los mismos marcadores. No se importa una clave SSH ni
se requiere acceso `full` al router: se utiliza la cuenta suministrada, con
host key fijada, y se muestran errores de permisos sin exponer credenciales.

Esta primera versión aprovisiona **laboratorios aislados**, y permite descubrir
routers de producción. La selección de interfaces de acceso de producción no
está implementada. No debe interpretarse el éxito del laboratorio como una
migración de abonados reales.

## Frontera del instalador

El proceso web se ejecuta sin privilegios y sin el socket del agente. El worker
`python manage.py run_network_jobs` comparte el mismo código y base de datos,
pero recibe un socket Unix exclusivo. El agente valida el UID del proceso
con `SO_PEERCRED`, una lista fija de operaciones y todos los parámetros; no
acepta comandos, rutas, scripts ni nombres de interfaz arbitrarios. Los nombres
`fi<ID>wg`, `fi<ID>tap` y `fi<ID>ppp` y direcciones se derivan de IDs 1–4095.
El rango reservado es `10.253.0.0/16` para túneles /30 y `10.254.0.0/16` para
pequeños pools de laboratorio. No se permite reutilizar interfaces ajenas.

Use `deploy/network/Dockerfile.agent` y `Dockerfile.radius`. El agente requiere
network namespace del host, capacidades NET_ADMIN/NET_RAW, `/dev/net/tun`,
`/dev/ppp`, volumen privado `/var/lib/fireisp-network`, volumen de socket
`/run/fireisp-network` y volumen de configuración `/var/lib/fireisp-radius`.
No requiere socket Docker, modo privileged ni acceso al filesystem del host.
Configure `NETWORK_WORKER_UIDS=1000`, `NETWORK_WORKER_GID=1000` con el UID/GID
real del worker. La instalación inicial usa el nodo `primary` en el mismo servidor.
Cada nodo tiene un único ejecutor activo: PostgreSQL serializa procesos del mismo
nodo y nodos diferentes trabajan en paralelo. Una reserva renovable y su generación
impiden continuar después de perder la propiedad de ejecución. Los trabajos
interrumpidos detienen su router hasta revisión y reintento explícito;
`--recover-stale` ya no los devuelve automáticamente a pendientes.

El selector de servidor al registrar un router permite asignarlo a otro nodo.
Mover un router ya aprovisionado exige revisar y cambiar su endpoint y estado
local; ese traslado no se automatiza. Consulte
[ubicación de servidores de red](network-nodes.md) y
[despliegue de funciones en otros servidores](distributed-deployment.md).

Variables:

- `NETWORK_NODE_ID`: nodo registrado que atiende el worker; `primary` por defecto.
- `NETWORK_PUBLIC_ENDPOINT`: IPv4 exterior del servidor principal heredado. Los
  planes de otros nodos usan el endpoint registrado en `NetworkNode`.
- `NETWORK_AGENT_SOCKET`: `/run/fireisp-network/agent.sock`.
- `NETWORK_RADIUS_TOKEN`: token del callback RADIUS. El token heredado solo
  autoriza el nodo `primary`; cada nodo adicional registra un token exclusivo por
  stdin y conserva el valor en su configuración privada. Django guarda el digest.
- `NETWORK_RADIUS_URL`: URL interna de Django, por ejemplo
  `http://127.0.0.1:18000/network/radius` en el namespace del host.

FreeRADIUS escucha únicamente en las direcciones privadas que el agente haya
configurado. El archivo de clientes contiene solo las IP /32 de los routers
registrados. El daemon consulta autorización/contabilidad en Django con token
exclusivo del nodo y devuelve las velocidades del plan actual. La API rechaza
NAS asignados a otro nodo aunque presenten ese token. Los archivos
se montan solo en agente/RADIUS; la web no accede a ellos. El proceso de entrada
de RADIUS valida la configuración antes de detener el daemon activo; si la
validación falla o vence su plazo, conserva el proceso existente. Reinicia únicamente su propio daemon
cuando cambia la generación. No se registran contraseñas ni se utiliza modo
`-X` de depuración. En producción, proteja igualmente el tráfico interno REST
con TLS o una red de contenedores privada cuando cruce un límite de confianza.

El firewall del proveedor debe permitir UDP `50000 + ID` hacia el VPS. La
aplicación configura en CHR UDP `55000 + ID`, limitado a la IPv4 del servidor.
El agente instala reglas INPUT propias con comentarios para UDP del túnel
limitado a la IPv4 del CHR, y GRE/ICMP/RADIUS desde el /32 privado en la
interfaz propia. La reversión retira exactamente esas reglas. No cambia
políticas globales, reglas ajenas ni rutas por defecto. El firewall externo
del proveedor sigue siendo un requisito cuando no hay API conectada.

## Recursos y reversión

El plan crea un WireGuard/par, dirección, reglas input limitadas a la ruta
privada, puente aislado, EoIP, puerto de puente, pool, perfil PPP, RADIUS
seleccionado por nombre de servicio y servidor PPPoE. No agrega el puerto WAN
ni puertos existentes al puente. Los recursos se identifican mediante
`comment=fireisp:<ID>:<tipo>`. Cambiar PPP AAA o recepción RADIUS es global;
se informa y exige aprobación adicional. Se conserva el valor previo y la
reversión lo restaura solo si sigue coincidiendo con el que aplicó FireISP.

Un fallo deja el trabajo y su diario revisables. La reversión recorre el diario
en sentido inverso, elimina únicamente recursos con el marcador propio y
retira la configuración privada del agente. No resetea el router. Tras una
reversión, haga un nuevo descubrimiento y revise un plan actualizado.

## Prueba real

El botón de prueba y `network_lab_onboard --phase lab --approve-lab` ejecutan el
mismo servicio. Se genera una credencial temporal con caducidad, se levanta un
TAP EoIP por el túnel WireGuard, se inicia `pppd` y se comprueba una sesión
real en `/ppp active`, IP asignada, ping a la puerta PPP y cola de velocidad.
Después verifica Accounting-Start, envía un Disconnect-Request RADIUS con
respuesta autenticada al suspender una suscripción demo, verifica salida de la
sesión y Accounting-Stop, y exige que una nueva autenticación sea rechazada.
Reanuda la suscripción con un cambio de plan y comprueba la velocidad nueva al
reconectar. La suscripción técnica nunca recibe `activated_at` ni inicia
facturación; queda cancelada al terminar. Al finalizar, detiene el cliente y
desactiva la credencial.
El TAP es efímero. El transporte EoIP userspace es exclusivamente de prueba,
no un dataplane de producción. No se simula una sesión exitosa si faltan
permisos, dispositivos, RADIUS o conectividad L2.

El resultado distingue enlace privado, sesión PPPoE, velocidad configurada,
contabilidad, desconexión y reconexión. No mide rendimiento ni valida acceso a
Internet, y lo informa expresamente. La cola de velocidad confirma la política
instalada, no la tasa medida bajo carga. Accounting-Interim se solicita cada
60 segundos; `--wait-interim` espera hasta 75 segundos y exige un Interim real.
Sin esa opción, el test corto exige Start/Stop y marca Interim como no probado.

`network_lab_onboard` ofrece fases `register`, `trust`, `discover`, `plan`,
`apply`, `verify`, `lab`, `status`, `rollback`. Registro recibe JSON por stdin
con host/user/password/is_lab; los secretos no se pasan como argumentos ni
se imprimen. Confiar exige la huella revisada; aplicar exige el hash exacto
del plan y `--approve-global` cuando corresponda.

Integración empresarial: `configure_subscription(subscription, router,
password, actor=None)` guarda acceso sin activar por sí mismo la suscripción.
`queue_subscription_sync(subscription_id, actor=None)` sincroniza su estado
deseado y encola desconexión de sesiones observadas al suspender/cancelar.
La autorización RADIUS rechaza nuevas conexiones de suscripciones no activas.
Cada cambio de estado incrementa una revisión de acceso: repetir una misma
suspensión reutiliza su trabajo, pero suspender después de reactivar crea uno
nuevo. Un trabajo de suspensión pendiente se descarta si el estado ya cambió.

## Referencias

- [Permisos y claves de RouterOS](https://help.mikrotik.com/docs/spaces/ROS/pages/8978504/User)
- [WireGuard RouterOS](https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/)
- [PPPoE RouterOS](https://manual.mikrotik.com/docs/virtual-private-networks/pppoe/)
- [EoIP RouterOS](https://manual.mikrotik.com/docs/virtual-private-networks/eoip/)
- [Formato EoIP documentado por la implementación amphineko](https://github.com/amphineko/eoip)
- [FreeRADIUS REST](https://www.freeradius.org/documentation/freeradius-server/4.0.0/howto/modules/rest/configuration.html)
- [FreeRADIUS: acciones de fallo y recuperación](https://wiki.freeradius.org/config/Fail-over)

## Autorización durante una caída de la aplicación

Cada diez segundos el worker publica un archivo completo de autorizaciones
confirmadas de los routers asignados a su nodo mediante `sync_entitlements`, una
operación cerrada del agente. Las instantáneas y los diarios de nodos distintos
no comparten archivos.
FreeRADIUS utiliza ese archivo únicamente cuando falla la consulta HTTP, nunca
para contradecir un rechazo explícito del servidor. Comprueba también la IP
origen del NAS y la caducidad de credenciales temporales. Un error de base de
datos o actualización mantiene la generación anterior. La caducidad de pago
no provoca suspensiones autónomas del worker: facturación debe confirmar el
estado deseado y congelar las suspensiones automáticas ante una caída del plano
de administración. La renovación de una autorización almacenada no depende de
que una persona tenga abierta una sesión web.

El archivo contiene contraseñas necesarias para PAP/CHAP y se guarda solamente
en el volumen privado de servicios RADIUS. Se monta en agente y RADIUS, nunca
en web. La interfaz de clientes no devuelve las contraseñas guardadas.

La contabilidad escribe primero un diario `detail` privado y persistente en
`/var/log/freeradius/fireisp-accounting`. Un lector del propio servicio RADIUS
reenvía bloques completos a la misma API interna, con campos permitidos y sin
seguir redirecciones. Solo avanza un cursor durable después de HTTP 204;
conserva el diario y el cursor ante fallos. La API tolera duplicados y mensajes
fuera de orden, conserva los mayores contadores y no reabre sesiones cerradas.
Las fechas del diario se preservan: un evento antiguo no se transforma en
evidencia reciente de instalación por haber sido recuperado hoy. La caída de
la API no se presenta como contabilidad en línea. La retención depende del
espacio de disco; el diario original no se elimina automáticamente.

Para verificar el respaldo, `network_lab_fallback --phase prepare --router-id
ID --approve-lab` crea una autorización temporal y publica la copia confirmada.
Un operador del despliegue coordina una interrupción breve de **solo web**;
`--phase test --router-id ID --job UUID` exige que la web esté realmente
indisponible y prueba PPP, IP, ping y cola con el RADIUS independiente. El comando
no controla contenedores ni servicios. Siempre restaure web en un `finally` y
ejecute `--phase cleanup` con el mismo ID/trabajo. La credencial vence en diez
minutos aun si el operador interrumpe el procedimiento. Después de restaurar,
`--phase verify-accounting` espera Start/Stop y una marca `replayed_at` que
confirma la entrega desde el diario. Esta marca de recuperación está separada
de la fecha original del evento. Los fallos del lector se registran sin datos
de abonados y como máximo una vez por minuto; los bloques permanecen pendientes
hasta una respuesta HTTP 204.

## Evidencia del laboratorio de staging

El 6 de septiembre de 2026 UTC se verificó RouterOS 7.21.5 con Paramiko 5.0.0 y
la huella SSH fijada. El trabajo `5e472922-d1dd-4066-b4c9-ccbc0ffa6abc` confirmó
sesión PPP real, IP, ping privado, cola 5/10 Mbps, Start/**Interim**/Stop,
suspensión con desconexión RADIUS y rechazo de acceso nuevo, y reconexión con
cola 5/20 Mbps. El trabajo `e5df0033-ea1c-41d1-a7b6-8ae0a2f55feb` autenticó
otra sesión real durante una interrupción web de 12,87 segundos mediante la
copia local y confirmó la recuperación automática posterior de Start/Stop.
El cursor terminó sin bytes pendientes de confirmación. Las 34 pruebas del
módulo pasaron, incluidas retención ante HTTP 503, redirecciones rechazadas,
bloques parciales, sustitución del archivo, cursor durable, campos secretos
excluidos y eventos duplicados/fuera de orden.

La revisión posterior conservó sin cambios el WireGuard/par, RADIUS, PPPoE,
cuatro perfiles, firewall, usuarios y grupos anteriores, además de ambos
ajustes globales PPP/RADIUS. No quedaron sesiones de prueba, credenciales
temporales habilitadas ni trabajos de laboratorio en ejecución. El estado
`network_sync=ok` verificó la aplicación/base de datos y ambos listeners
privados. Esta evidencia no certifica acceso a Internet, rendimiento bajo
carga, radio/CPE de campo, interfaces de acceso de producción ni una migración
de abonados reales; siguen siendo puertas de aceptación de la siguiente etapa.
El benchmark de facturación con 20.000 abonados no es una prueba de 20.000
sesiones PPP ni de carga de autenticación/contabilidad. La instantánea de
autorizaciones admite hasta 25.000 entradas, con límites independientes de
8 MiB por solicitud IPC (UTF-8, incluida la nueva línea) y 16 MiB para el archivo
RADIUS generado. Los límites de cantidad y bytes se aplican conjuntamente;
credenciales más largas consumen más espacio. El worker lee las credenciales
en lotes de 500 y en orden estable. Rechazar una instantánea conserva el archivo
y la generación anteriores. Las pruebas locales publican 20.000 entradas
serializadas con usuarios de 64 caracteres y contraseñas de 128, comprueban el
límite de 25.000 y rechazan cantidad/bytes excedidos sin escrituras parciales.
Esto verifica tamaño y publicación de configuración; la carga concurrente de
autenticación y contabilidad todavía requiere su propia prueba de capacidad.
