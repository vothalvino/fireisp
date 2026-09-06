# Staged implementation plan

Target: one operator, fixed wireless in Cuauhtémoc, Chihuahua; Spanish UI,
MXN, America/Chihuahua, prepaid monthly cash and transfer collection.
The application is a modular Django monolith with one database and explicit
service boundaries. This keeps deployment and maintenance manageable for one
developer while separating fiscal, network, billing and compliance behavior.

## Stage 1 — Foundation and customer lifecycle

Implemented: MIT repository, Ubuntu installer, HTTPS, PostgreSQL, access roles,
one-use account invitations, branches, customers, immutable audit, plans,
service registration and actual activation date. The customer portal isolates
each account. Production activation has explicit readiness gates.

Exit checks: migration from empty database, role/ownership/CSRF tests,
registration-to-installation browser flow, persistent deployment health.

## Stage 2 — Prepaid billing and fiscal demonstration

Implemented: monthly anniversary charges, advance credit, FIFO allocations,
cash/transfer collection, reconciliation, cash closing, reversals, outage
credits/refunds, reviewed suspension policy and paid renewal. Finkok DEMO
implements PUE, PPD, Pagos 2.0, egreso, global invoices and uncertain-request
recovery. Cancellation requests remain pending until the authority confirms.

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

Exit checks: actual PPP session evidence, private tunnel, speed-policy readback,
accounting, disconnect/reconnect, explicit failure states and verified restore.
The short lab test does not establish Internet throughput or radio performance.

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
fiber/OLT support and multi-server failover can follow the validated pilot.
These are separate increments with their own acceptance tests, not implied
capabilities of the current demonstration release.
