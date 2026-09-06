# FireISP

Modular ISP management for a fixed-wireless operator in Cuauhtémoc, Chihuahua,
Mexico. Python/Django, Spanish interface, MXN, prepaid monthly service, Finkok
and MikroTik PPPoE/FreeRADIUS. One organization can operate multiple branches.

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
- Ubuntu installer, HTTPS, persistent storage, encrypted backups, isolated restore
  verification, health diagnostics and PostgreSQL CI tests.

This is a **demonstration/pilot release**, not an authorization to operate an ISP
or issue production tax documents. Production credentials, operator-specific
legal evidence and the real access-network design are separate launch gates.
See the [staged plan](docs/roadmap.md), [test evidence](docs/testing.md),
[legal baseline](docs/legal-baseline.md), [fiscal behavior](docs/fiscal.md)
and [network boundaries](docs/network.md).

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

From an inspected checkout on Ubuntu 24.04:

```bash
sudo python3 deploy/install.py --hostname isp.example.com --public-ip 203.0.113.10 --demo-data
```

Replace the example hostname/IP with your server. Configure DNS and provider
firewall access first. The installer generates private application credentials
and writes the administrator invitation to `/etc/fireisp/first-login.txt`.
See [installation, upgrades and recovery](docs/deployment.md).

## License

[MIT](LICENSE). Dependencies retain their respective licenses. Never commit
passwords, environment files, PAC tokens, router credentials or private keys.
