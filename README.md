# FireISP

Modular ISP management for a fixed-wireless operator in Cuauhtémoc, Chihuahua,
Mexico. Python/Django, Spanish interface, MXN, prepaid monthly service, Finkok
and MikroTik PPPoE/FreeRADIUS. One organization can operate multiple branches.

The default installation runs on **one server**. As the ISP grows, the same
release can run billing, fiscal, core-event or network work on additional servers;
the existing customer accounts and shared PostgreSQL/Redis remain in place.

## What is implemented

- Customers, versioned plans, service activation, staff roles and customer portal.
- Prepaid charges, cash payments, reversals, bank CSV reconciliation, cash closures,
  outage credits, refunds, reviewed suspensions and paid renewal.
- Finkok **DEMO**: issuer verification, CFDI 4.0 PUE/PPD, payment complement 2.0,
  credit notes, global invoices, XML/PDF, cancellation requests and recovery.
- Tickets, installation orders, sites, sectors, CPE inventory and radio evidence.
- Contracts, tariff-registration evidence, privacy/consent records, ARCO requests,
  notices, cancellation folios and reviewed retention workflows.
- Software-managed CHR discovery, pinned SSH identity, reviewed provisioning,
  private WireGuard link, isolated PPPoE laboratory, RADIUS and job audit trail.
- Separate execution roles for web, core events, billing, fiscal, scheduling and
  network work, with queue routing, release checks and instance heartbeats.
- Assigned network nodes with local RADIUS/agent state, scoped callback tokens and
  serialized provisioning; existing routers remain on the `primary` node.
- Ubuntu installer, HTTPS, persistent storage, encrypted backups, isolated restore
  verification, health diagnostics and PostgreSQL CI tests.

This is a **demonstration/pilot release**, not an authorization to operate an ISP
or issue production tax documents. Production credentials, operator-specific
legal evidence and the real access-network design are separate launch gates.
See the [staged plan](docs/roadmap.md), [test evidence](docs/testing.md),
[legal baseline](docs/legal-baseline.md), [fiscal behavior](docs/fiscal.md)
and [network boundaries](docs/network.md).

## Deployment architecture

The Django business modules share one code release and database. Execution roles
can be placed independently: move fiscal processing when document work becomes
heavy, add billing workers when its queue grows, or add network nodes near new
routers. PostgreSQL and Redis stay shared dependencies. Moving a role does not
create a separate ISP installation or independently versioned business service.

Network nodes own their local agent, tunnels, RADIUS cache and accounting journal.
New routers can be assigned to an additional node. Moving an already provisioned
router requires a reviewed network cutover; starting another worker does not move
its interfaces or WireGuard endpoint. Additional execution capacity also does not
provide automatic database or broker failover.

See [growing from one server to several](docs/distributed-deployment.md) for role
installation, private connectivity and rollback, and
[network server placement](docs/network-nodes.md) for assignment and cutover limits.

## Local development

Python 3.12 is required. Use a virtual environment. On systems without
`ensurepip`, create it with `uv venv --python 3.12 .venv` and install with
`uv pip install --python .venv/bin/python --require-hashes -r requirements.lock`
in place of the first two commands below.

```bash
python3.12 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.lock
export DEBUG=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py bootstrap --url http://127.0.0.1:8000 --invitation-file /tmp/fireisp-first-login.txt
.venv/bin/python manage.py seed_demo
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Open the one-use link in the protected invitation file to set the administrator
password. Local development uses SQLite; deployment and concurrency tests use
PostgreSQL. Demo data contains fictitious customers and plans. It does not
invent completed installations, payments or network tests.

```bash
DEBUG=true .venv/bin/python manage.py test --noinput
.venv/bin/ruff check .
DEBUG=true .venv/bin/python manage.py makemigrations --check --dry-run
```

## Deploy

Run this on an Ubuntu 24.04 server:

```bash
curl -fsSL https://raw.githubusercontent.com/vothalvino/fireisp/main/install.sh | sudo bash
```

Choose **Main server** for the first installation, then select local billing,
Finkok/PDF and network modules. All three are selected initially, so the ISP can
start on one server. Configure its domain and inbound TCP 80/443 first.

Later, run the same command on another server and choose **Additional server**.
Select its execution modules and enter the main server's SSH connection details.
The wizard verifies the SSH host identity, enrolls a restricted connection key,
and maintains an encrypted tunnel using the main server's exact application
release. No database passwords need to be copied manually. Allow the additional
server's IP through the main server's SSH firewall.

These selections control where background work runs; customers, plans and other
application features remain part of the shared application. See
[installation and recovery](docs/deployment.md) and
[adding or moving modules](docs/distributed-deployment.md). The first administrator
invitation is stored privately at `/etc/fireisp/first-login.txt` on the main server.

## License

[MIT](LICENSE). Dependencies retain their respective licenses. Never commit
passwords, environment files, PAC tokens, router credentials or private keys.
