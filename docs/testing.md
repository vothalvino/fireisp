# Acceptance evidence

Executed on 5 September 2026 (America/Chihuahua), using fictitious customers and
the authorized CHR/Finkok demonstration environments. No real subscriber
traffic or production fiscal credentials were used.

## Automated checks

Final suite: **136 tests passed on PostgreSQL on the VPS** (10.936 seconds).
The local suite passed with the two PostgreSQL-only cases skipped. Ruff and
migration consistency checks passed. The complete installer was rerun on the
VPS and finished only after trusted HTTPS application/database verification.

The suite runs against a dedicated PostgreSQL database on the VPS and in GitHub
Actions. It includes payment idempotency/concurrency, activation/payment lock
ordering, ledger invariants, fiscal sequencing, access ownership, CSRF, role
checks, legal gates, deadlines, retention review and provisioning failure cases.
Local SQLite runs skip the two PostgreSQL-specific concurrency tests.

Commands:

```bash
DEBUG=true .venv/bin/python manage.py test --noinput
.venv/bin/ruff check .
DEBUG=true .venv/bin/python manage.py makemigrations --check --dry-run
sudo python3 deploy/check.py test --image fireisp:staging
```

The server checker creates and removes dedicated test databases; it never runs
regression fixtures against the live customer database. Tests use an isolated
cache/broker configuration.

## Live integration results

| Check | Observed result |
| --- | --- |
| Public deployment | Trusted HTTPS, HTTP redirect, application/database health, private database and callback ports. |
| Browser | Staff customer → service → cash payment → installation activation; desktop/mobile layout; bounded customer lookup; multiple affected services preserved across search, save and edit. Temporary QA accounts disabled after testing. |
| Customer portal | Own payments only; support folio/deadline; ARCO receipt and identity-verification state; service cancellation folio and retained document access. Another customer's fiscal PDF denied. |
| Authenticated GET smoke | 62 staff requests: 61 successful pages/downloads and one expected router-review redirect; no database writes. |
| Finkok from VPS | Issuer verification and actual CFDI DEMO stamping. UUID `6806AF8A-AE0D-5BC7-9098-143A06FB00E0`; XML, acuse and valid 19,148-byte PDF; ledger balance zero. |
| Fiscal variants | Real DEMO PUE, PPD, partial Pagos 2.0, related egreso, final payment with zero remaining balance, and two-ticket global invoice. |
| Cancellation | Signed request returned 201 and acuse. SAT query returned `No Encontrado`; application correctly preserves `cancel_pending`, not final cancellation. |
| CHR private link | Pinned SSH identity, WireGuard handshake and private ping; pre-existing router configuration preserved. |
| Real PPPoE | Assigned IP, router session and gateway ping; Accounting Start/Stop; authenticated disconnect; suspension rejected new CHAP login; resume/reconnect succeeded. |
| Plan speed policy | Observed upload/download queue limits 5/10 Mbps, then 5/20 Mbps after the controlled plan-change test. This is configuration readback, not a throughput measurement. |
| Entitlement directory | A 20,000-entry serialized fixture (5,860,060 bytes) published successfully; exact 25,000-entry boundary accepted and 25,001 rejected while preserving the previous snapshot. This is not a concurrent AAA load test. |
| Management outage | Web unavailable was verified; a real PPP session authenticated using confirmed local RADIUS access records. Web restarted in a finally block and temporary credentials were disabled. |
| Accounting recovery | The durable journal automatically delivered the earlier outage's Start/Stop records after recovery. Replay confirmation is recorded separately from original event times; closed sessions stay closed. A later real PPP test also captured Accounting-Interim. |
| Encrypted backup | Archive decrypted and every archived file hash verified; PostgreSQL restored in a disposable container without network/host mounts; exact snapshot counts matched. |
| Off-host recovery copy | Ciphertext and checksum report copied and verified off the VPS; recovery identity stored separately in protected local storage. Recurring off-host replication is not configured. |
| Source/dependencies | Secret scan found no committed-source secrets; dependency audit reported no known vulnerabilities after updating Paramiko to 5.0.0. |

The final encrypted restore drill used the 2026-09-06 01:47:48 UTC snapshot:
16 files, 8 fictitious customers, 2 invoices, 2 payments, 68 audit records and
38 applied migrations. The archived journal ended at a complete record and
its replay cursor was reset; the live journal/cursor were preserved. The
replacement database was disposable and the running database was untouched.

The normal CHR regression with Interim job `5e472922-d1dd-4066-b4c9-ccbc0ffa6abc`
and final outage/recovery job `e5df0033-ea1c-41d1-a7b6-8ae0a2f55feb` preserve detailed
results in the application's network job history. Failed development attempts
remain distinguishable from subsequent verified successes.

## 20,000-customer billing benchmark

The VPS has four vCPUs and approximately 3.7 GiB RAM. The benchmark process was
limited to 1.5 CPUs and 650 MiB, using a disposable PostgreSQL database.

- 20,000 synthetic customers and subscriptions.
- Actual billing service created 20,000 charges in **169.66 seconds**.
- Actual payment/allocation service applied 20,000 payments in **177.07 seconds**.
- Total: **346.72 seconds** (5 minutes 47 seconds).
- Zero unpaid invoices; zero missing paid-through dates; expected counts matched.
- No PAC calls. Fixture creation and database migration were outside the timed
  charge/payment path.

This passed the 30-minute billing target. It is not a concurrent-user web load
test, a 20,000-session RADIUS test, a radio-capacity test or a PAC throughput
commitment.

## Limits of the evidence

The demo verifies the implemented pilot workflows. It does not establish real
subscriber Internet throughput, production router interface migration, final
SAT cancellation, production legal readiness or whole-server failover. The
[launch stage](roadmap.md) identifies the operator evidence and reviewed
production changes still required.

Django's deployment check reports the two optional HSTS subdomain/preload
settings as unset. HTTPS and HSTS apply to the configured staging hostname;
the deployment does not enroll the domain in a browser preload list.
