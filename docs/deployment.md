# Installation, upgrades and recovery

## One-line installation

On Ubuntu 24.04, connect through an interactive SSH terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/vothalvino/fireisp/main/install.sh | sudo bash
```

Choose **Main server** for the first host and select the modules to run locally.
Billing, Finkok/PDF and network processing are selected initially. The main web
application, database, broker, core events and scheduler are always installed.
Module selection controls execution placement, not which screens or business
features the shared application contains.

On a later host, use the same command and choose **Additional server**. Select
one or several execution modules, supply the main server's SSH hostname/IP and
port, and authenticate for enrollment. The wizard installs the matching release
and creates a persistent restricted SSH tunnel automatically. See
[additional-server setup](distributed-deployment.md#connect-an-additional-server)
for firewall requirements and moving existing work.

The bootstrap downloads one exact Git commit into a protected, read-only release
directory under `/opt/fireisp/installer/releases`. It preserves terminal prompts
even when piped into Bash. `--help` displays its options; an inspected older
release can be selected with `sudo bash -s -- --release FULL_GIT_SHA` at the end
of the command. Installation choices are saved only after success in the
root-only `/etc/fireisp/wizard.json`. Reruns offer the current local selection.

## Architecture

The staging application runs at `https://demo2.opentrk.com.mx` on Ubuntu 24.04,
4 vCPUs and approximately 3.7 GiB RAM. `/opt/fireisp/app` points to the inspected
release after a wizard installation; Compose configuration is in
`/opt/fireisp/staging`.

Caddy terminates HTTPS. Django/Gunicorn serves the application; PostgreSQL 17
stores business state. Valkey and Celery deliver durable outbox events and
scheduled work. A separate network worker calls a restricted Unix-socket agent.
FreeRADIUS authenticates subscribers on private tunnel addresses. Application
containers have memory limits; the web process is unprivileged, read-only,
and has neither the Docker socket nor the provisioning-agent socket.

Only HTTP/HTTPS are publicly published by Compose. PostgreSQL, Valkey and the
application callback use loopback-only listeners at `127.0.0.1:15432`,
`127.0.0.1:16379` and `127.0.0.1:18000`. Additional servers reach these listeners
through their restricted SSH tunnels. Docker Engine 28 or newer is required
before installing this layout.
Caddy rejects public `/network/radius/` requests. RADIUS listeners and allowed
NAS addresses are managed by the network module. Provider firewall rules are
external prerequisites when no provider API is connected.

## Fresh installation

Prepare Ubuntu 24.04 with working SSH access, DNS pointing at its public IPv4,
outbound HTTPS and inbound TCP 80/443. Any AAAA record must also reach this host.
Keep SSH access available. The installer supports this OS version and checks
for at least 5 GiB of free space. The tested sizing is four vCPUs and 4 GiB RAM. Before changing the host, the
installer verifies the hostname resolves to the supplied IPv4 and reports any
observed conflicting AAAA records. Completion requires verified public HTTPS
and both application/database readiness flags.

Use the one-line wizard above and choose **Main server**. It asks for the domain,
public IPv4 and local module selection. For an advanced scripted installation
from an inspected checkout, the underlying installer remains available:

```bash
sudo python3 deploy/install.py --hostname isp.example.com --public-ip YOUR_PUBLIC_IPV4 --local-workers billing,fiscal,network
```

Replace the example values. The installer installs Docker Engine/Compose from
Docker's Ubuntu repository when needed, installs `age`, prepares PPP devices,
generates secrets, validates Compose and Caddy, builds containers, migrates the
database and initializes the organization, roles and administrator invitation.
Reruns preserve generated secrets and update hostname/source configuration.
The underlying command's `--demo-data` option is optional and idempotent.

The administrator invitation is root-only at `/etc/fireisp/first-login.txt`,
expires after 24 hours and is usable once. The administrator sets their own
password. Never publish this file or commit it. If it expires, explicitly renew
the invitation through `manage.py bootstrap --renew-invitation`, using the same
protected bind mount as the installer.

## Operating the application

```bash
cd /opt/fireisp/staging
sudo docker compose ps
sudo docker compose exec -T web python manage.py diagnose
curl --fail --silent --show-error https://demo2.opentrk.com.mx/healthz
```

`/healthz` reports application/database availability, not legal, fiscal or
subscriber readiness. Sign in and open **Ajustes → Estado del sistema** for
integration results, backup freshness and worker failures. The application
keeps the environment visibly marked as demonstration. Public HTTP redirects
to HTTPS; certificate validation uses the normal trust store. Caddy retains
its ACME state in named volumes and renews certificates automatically.

The Caddy administration endpoint is disabled. Validate changed configuration
before recreating Caddy; `caddy reload` is not available here.

Caddy is built from the pinned source and patched Go toolchain in
`deploy/staging/Dockerfile.caddy`. The installer and CI inspect the compiled
runtime before accepting it. See [HTTPS build provenance and updates](caddy-security.md).

Login attempts use shared Valkey counters: at most 10 attempts per account and
80 per source address in a fixed 15-minute window. Blocked requests never check
passwords; a successful login resets the account counter. Restarting web workers
does not clear these shared limits.

## Upgrades

Take and verify a backup before applying an inspected release. Preserve the
previous source revision and image digest. Run the installer against the new
checkout to rebuild and migrate; it does not reset application or router state.
Reruns may briefly interrupt management requests. Schedule router provisioning
outside that window.

If additional servers are connected, first drain their execution roles as
described in the [distributed upgrade prerequisites](distributed-deployment.md#prerequisites).
After upgrading the main server, rerun the one-line wizard on each additional
server. It obtains the main server's exact release rather than independently
upgrading a worker to the newest repository commit.

Schema downgrades are not automatic. If a migration is incompatible with the
previous release, recover into a separate instance from the verified pre-upgrade
backup and switch traffic only after checking it. Do not remove named volumes
or use `docker compose down -v` for an upgrade.

## Backups and verification

`fireisp-backup.timer` runs every 15 minutes. The installed script is
`/etc/fireisp/backup.py`; encrypted archives and their non-secret reports are in
`/var/backups/fireisp`. The first run creates a root-only age recovery identity
at `/etc/fireisp/backup.agekey`. Keep a separate off-host copy of this identity.
The encrypted archive includes the deployment environment, PostgreSQL dump,
private documents, network state, RADIUS configuration and accounting journal.

```bash
sudo python3 /etc/fireisp/backup.py create-and-verify
sudo python3 /etc/fireisp/backup.py verify --file /var/backups/fireisp/fireisp-TIMESTAMP.tar.age
sudo systemctl status fireisp-backup.timer
```

Verification decrypts into a private temporary directory, validates the archive
allowlist and every file hash, then restores PostgreSQL into a disposable
container with **no network, published ports or host mounts**. Expected counts
come from the same exported database snapshot as the dump. The test compares
customers, invoices, payments, audit events and migrations and removes only its
own labeled container. It never restores over the running database.

Retention keeps 96 recent points plus one per day for 30 days; only owned,
checksum-verified archive/report pairs are eligible for deletion. This is a
recovery policy, not legal erasure of every historical copy. A 15-minute timer
is a schedule, not a guarantee of recovery point or recovery time.

A verified off-host copy is part of staging acceptance. Continuous off-host
replication needs an operator-controlled destination and retention policy; the
installer does not silently create an external storage account.

## Recovery after loss of a server

1. Obtain a verified encrypted archive, its checksum report and the separately
   stored age identity. Verify ciphertext integrity before decryption.
2. Prepare an isolated replacement Ubuntu host and the matching source release.
   Decrypt into a root-only directory and inspect `manifest.json`. Preserve the
   archived `ENCRYPTION_KEY`; without it, database credentials for integrations
   cannot be decrypted.
3. Run the isolated verification command first. Restore the environment,
   database dump and named volume contents to the replacement deployment while
   its application/worker/RADIUS services remain stopped.
4. Start PostgreSQL and restore with `pg_restore --exit-on-error`; restore file
   ownership for documents and network volumes. Start application services and
   run `diagnose`, an authenticated browser check and a private-link check.
5. Reconcile any payments, PAC requests and network jobs made after the backup.
   An uncertain fiscal request must be recovered against the PAC before retry.
6. Switch DNS/network endpoints only after review. Do not run two provisioning
   workers against the same router identity at once.

The automated drill validates backup contents and database restoration. It
does not claim an unattended, zero-downtime whole-server failover.

## Private material

`/opt/fireisp/staging/.env` and `/etc/fireisp` are protected and excluded from
Git. Finkok and router credentials are encrypted in application storage.
The public repository contains no deployment password or private certificate.
See [fiscal setup](fiscal.md) and [router onboarding](network.md) for the product
workflows. Never print the environment or private JSON files in diagnostics.
