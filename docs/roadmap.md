# Staged implementation plan

Target: one operator, fixed wireless in Cuauhtémoc, Chihuahua; Spanish UI,
MXN, America/Chihuahua, prepaid monthly cash and transfer collection.
Start with one Ubuntu server. The business modules share a Django release and
PostgreSQL database; web, core-event, billing, fiscal, scheduler and network
execution have separately placeable roles. Redis carries the routed work queues.
This keeps the initial installation manageable for one developer and provides a
path to move heavy execution onto additional servers without creating another
ISP database. Roles must use the same declared release and shared services.

## Stage 1 — Foundation and customer lifecycle

Implemented: MIT repository, Ubuntu installer, HTTPS, PostgreSQL, access roles,
one-use account invitations, branches, customers, immutable audit, plans,
service registration and actual activation date. The customer portal isolates
each account. Production activation has explicit readiness gates. The default
Compose installation keeps all roles together; role-specific queues, runtime
heartbeats and release checks also support later placement on separate hosts.

Exit checks: migration from empty database, role/ownership/CSRF tests,
registration-to-installation browser flow, persistent deployment health.

## Stage 2 — Prepaid billing and fiscal demonstration

Implemented: monthly anniversary charges, advance credit, FIFO allocations,
cash/transfer collection, reconciliation, cash closing, reversals, outage
credits/refunds, reviewed suspension policy and paid renewal. Finkok DEMO
implements PUE, PPD, Pagos 2.0, egreso, global invoices and uncertain-request
recovery. Cancellation requests remain pending until the authority confirms.
Fiscal requests run as durable jobs on the fiscal queue so their execution can
move to a dedicated server; documents remain retrievable from the web role.

Exit checks: decimal accounting, idempotency/concurrency, real PAC demo
documents, owned XML/PDF downloads and a 20,000-customer billing benchmark.

## Stage 3 — Wireless operations and Mexican compliance

Implemented: tickets, installation orders, sites/sectors/CPE, radio and permit
evidence, approved contract versions, registered tariff snapshots, customer
consent, contract notices, ARCO deadlines, cancellation folios, legal holds and
reviewed retention deletion for supported records.

The [legal baseline](legal-baseline.md) maps the 2025 telecom/data-protection
changes and current fiscal rules to software controls. A legal workflow stores
evidence; it cannot confer a concession, register a contract or certify a radio.

Exit checks: activation blocked by missing evidence, complaint/outage suspension
holds, calendar deadlines, same-channel cancellation and immutable consent.

## Stage 4 — Software-managed network laboratory and recovery

Implemented: CHR identity verification/discovery, reviewed idempotent plans,
WireGuard, isolated EoIP/PPPoE test client, RADIUS authorization/accounting,
desired-state jobs, scoped rollback and degraded-management authorization.
The web process cannot issue arbitrary host/router commands. Backup and
isolated restore verification are provided by the installer.

Network execution is assigned by node. Existing routers stay on `primary`; new
routers can use an additional registered node with its own endpoint, local
agent/RADIUS state and scoped API token. Provisioning and cache updates have one
active executor per node. Interrupted effects require review before retry.

Exit checks: actual PPP session evidence, private tunnel, speed-policy readback,
accounting, disconnect/reconnect, explicit failure states and verified restore.
The short lab test does not establish Internet throughput or radio performance.

## Capacity expansion when measurements justify it

Keep every role on the first server until CPU, memory, queue delay or network
placement requires a change. Then move one heavy role using the same release and
private access to shared PostgreSQL/Redis. Verify its heartbeat and representative
jobs, drain the old worker, and retain a rollback path before moving another role.
A network node can take newly assigned routers; moving provisioned routers is a
separate reviewed endpoint/state cutover, currently manual.

See [distributed deployment](distributed-deployment.md) and
[network nodes](network-nodes.md). Acceptance requires real role-to-role job and
artifact retrieval tests plus failure/recovery checks. Worker expansion does not
remove the database/broker dependency or establish an unmeasured subscriber limit.

## Stage 5 — Operator acceptance and production launch

Requires operator attention after the demo is accepted:

- Actual legal identity, concession/authorization and operating scope, registered
  contract/tariffs, privacy documents and installation/permit/radio evidence.
- Production PAC account/CSD and fiscal review. This release deliberately uses
  Finkok DEMO endpoints; production enablement is a separate reviewed release.
- Actual access interfaces/VLANs, address pools and network design. Automated
  creation currently targets an isolated lab; production router discovery and
  subscriber records do not authorize moving existing interfaces.
- Off-host backup destination, retention ownership and a recovery rehearsal on
  replacement infrastructure. Single-server staging is not high availability.

Pilot with a small controlled subscriber group after these gates, reconcile
every charge and session, then grow in measured batches. The 20,000-account
database benchmark is not a 20,000-concurrent-PPP or radio-capacity test.

## Later expansion

Production provisioning templates, scheduled contract/plan migrations, more
payment channels, automated external notification delivery, inventory purchasing,
fiber/OLT support and automatic database/broker failover can follow the validated pilot.
These are separate increments with their own acceptance tests, not implied
capabilities of the current demonstration release.
