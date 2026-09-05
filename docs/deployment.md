# FireISP staging infrastructure

The target is `demo2.opentrk.com.mx`, an Ubuntu 24.04 VPS with 4 vCPUs,
approximately 3.7 GiB RAM, and 116 GB disk. Deployment files live on the server
at `/opt/fireisp/staging`.

This deployment provides infrastructure only: Caddy, automatic HTTPS, a
maintenance response, and an infrastructure health endpoint. Django, customer
data, Finkok integration, and router integration are not installed. A successful
health check does not mean the FireISP application exists or is ready.

## Verified staging status

Public HTTPS was verified on 2026-09-05 after inbound TCP ports 80 and 443
were opened in the provider firewall. The certificate chain and hostname
validated successfully using the client's normal trust store, with TLS 1.3
and a Let's Encrypt certificate. Caddy manages renewal using its persistent
data volume.

External checks passed: HTTP redirects to HTTPS with status 308;
`GET /healthz` returns status 200 and `application_ready: false`; the root
page returns the intentional maintenance status 503 with `Retry-After: 300`.
The HTTPS certificate blocker is resolved. Application implementation and
off-host backups are still pending.

The CHR has been inspected without configuration changes. WireGuard tools are
installed on the VPS, but no tunnel is configured or active. Further router
and private-network provisioning belongs in the application's
[onboarding workflow](onboarding.md).

## Service behavior

- HTTP redirects to HTTPS. Caddy manages certificates automatically for the
  configured hostname.
- `GET https://demo2.opentrk.com.mx/healthz` returns HTTP 200 and
  `{"environment":"staging","application_ready":false,"infrastructure_ready":true}`.
- Other HTTPS requests, including `/`, return HTTP 503 and
  `FireISP: entorno de pruebas en preparación.`, with `Retry-After: 300`.
- Health and maintenance responses include `Cache-Control: no-store` and
  `X-Robots-Tag: noindex, nofollow`. The health response has no `Retry-After`.
- Only TCP ports 80 and 443 are published by Compose, on all host interfaces.
  HTTP/1.1 and HTTP/2 are enabled; HTTP/3 and UDP publishing are disabled.
- The Caddy administration endpoint is disabled. Apply configuration changes by
  recreating the container; `caddy reload` cannot work in this configuration.
- Caddy is limited to 192 MiB RAM and 0.5 CPU, restarts unless manually stopped,
  and uses Docker's `local` log driver with three 10 MB log files.

## Prerequisites

Install Docker Engine and the Docker Compose plugin from Docker's official
Ubuntu repository. Configure SSH access separately. The operator needs sudo
access, and the server needs working DNS and outbound HTTPS access for image
downloads and certificate issuance. The hostname's A record, and any AAAA
record, must point to this server. Allow inbound TCP 80 and 443 in both the
provider firewall and host configuration; retain the configured SSH access.

Docker port publishing can bypass UFW rules. Restrict future database and
internal service ports to private container networks; do not publish them on
public interfaces. If temporary host access is necessary, bind explicitly to
loopback and use an SSH tunnel.

`STAGING_HOSTNAME` defaults to `demo2.opentrk.com.mx` in Compose. To override it,
create `/opt/fireisp/staging/.env` on the server with a line such as
`STAGING_HOSTNAME=staging.example.com`, and configure that hostname's DNS first.
Keep this file outside version control.

## Initial deployment

Run these commands from the repository root on your workstation. Set
`STAGING_SSH_TARGET` to the SSH alias or user/hostname already configured by the
operator; SSH credentials and private key locations are not stored here.

```bash
STAGING_SSH_TARGET='your-configured-ssh-alias'
ssh "$STAGING_SSH_TARGET" 'mkdir -p ~/fireisp-staging-upload'
scp deploy/staging/compose.yaml deploy/staging/Caddyfile \
  "$STAGING_SSH_TARGET:fireisp-staging-upload/"
ssh "$STAGING_SSH_TARGET"
```

On the server:

```bash
sudo install -d -m 0755 /opt/fireisp/staging
sudo install -m 0644 ~/fireisp-staging-upload/compose.yaml /opt/fireisp/staging/compose.yaml
sudo install -m 0644 ~/fireisp-staging-upload/Caddyfile /opt/fireisp/staging/Caddyfile
cd /opt/fireisp/staging
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo docker compose up -d
```

The official image is pinned to a reviewed digest in `compose.yaml` for
reproducible deployments, using `caddy:2-alpine@sha256:...`. For an upgrade,
obtain the next digest from the image actually pulled; do not invent or reuse
an unverified digest:

```bash
sudo docker image inspect caddy:2-alpine --format '{{json .RepoDigests}}'
```

Certificate issuance may take a short time after the first start. The named
volumes `fireisp-staging_caddy_data` and `fireisp-staging_caddy_config` preserve
certificate material and Caddy state across container replacement.

## Validation

On the server:

```bash
cd /opt/fireisp/staging
sudo docker compose ps
sudo docker compose logs --tail=100 caddy
sudo docker compose exec caddy caddy version
sudo ss -lntup
```

From the workstation, check public DNS, TLS, HTTP redirection, and both response
types. Substitute the hostname if it was overridden:

```bash
curl --silent --show-error --head http://demo2.opentrk.com.mx/
curl --silent --show-error --include https://demo2.opentrk.com.mx/
curl --fail --silent --show-error --include https://demo2.opentrk.com.mx/healthz
```

Expect an HTTPS redirect from HTTP, a 503 maintenance response with
`Retry-After: 300` at `/`, and 200 JSON with `application_ready: false` at
`/healthz`. Inspect the cache and robots headers on the HTTPS responses. Do not
use curl's `--fail` for the maintenance request: HTTP 503 is intentional. Do not
use `--insecure`; a trusted certificate is part of this validation.

## Updates and recovery

Before copying an update into `/opt/fireisp/staging`, preserve the current
deployment files on the server:

```bash
cd /opt/fireisp/staging
sudo cp -p compose.yaml compose.yaml.previous
sudo cp -p Caddyfile Caddyfile.previous
```

Upload the revised files using the initial deployment's `scp` command, install
them into `/opt/fireisp/staging`, then apply them:

```bash
cd /opt/fireisp/staging
sudo docker compose config --quiet
sudo docker compose pull
sudo docker compose run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo docker compose up -d --force-recreate caddy
sudo docker compose logs --tail=100 caddy
```

Re-run the public validation commands. An image upgrade requires updating the
pinned digest after reviewing and pulling the intended official image; pulling
an existing digest does not upgrade it. Container recreation causes a short
interruption because only one Caddy instance runs.

If validation fails before recreation, restore the saved files and investigate
without replacing the running container. If an applied update fails, restore
the previous configuration and image reference:

```bash
cd /opt/fireisp/staging
sudo cp -p compose.yaml.previous compose.yaml
sudo cp -p Caddyfile.previous Caddyfile
sudo docker compose config --quiet
sudo docker compose run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo docker compose up -d --force-recreate caddy
sudo docker compose logs --tail=100 caddy
```

For an unchanged configuration after a process failure, use
`sudo docker compose restart caddy`, then repeat validation. For certificate
problems, check DNS, clock synchronization, reachable TCP 80/443, and the Caddy
logs before changing certificate state.

If ACME validation reports a connection timeout while the workstation can
reach the server, inspect the provider firewall as well as Ubuntu. Certificate
authorities connect from other networks: TCP 80/443 must be publicly reachable,
not limited to the workstation's source address. Confirm the challenge packets
reach the host before changing working Docker forwarding rules. Do not bypass
certificate verification to treat an incomplete HTTPS setup as successful.

Preserve the named volumes. Do not run `docker compose down -v`, remove the
Caddy volumes, or prune volumes as part of deployment or recovery. Certificates
and private keys live in the data volume. Backups are not yet configured; the
`.previous` files are a local rollback aid, not a backup. Configure and verify
off-host backups before adding application state or customer data.

Never commit passwords, SSH private keys, CSD certificates or keys, Finkok
credentials, router credentials, or other external credentials. Add future
secrets through a separately managed deployment mechanism.

## Finkok demo credential preparation

The staging VPS stores the supplied demo credentials in
`/etc/fireisp/finkok-demo.json`, outside the deployment directory and repository.
The directory is owned by root with mode `0700`; the file is owned by root with
mode `0600`. Its JSON fields are `environment` (fixed to `demo`), `username`,
`token`, and `issuer_rfc`. Inspect permissions with `stat`; do not print the file in terminal
logs. The credentials are not mounted into Caddy or used by an application yet.

Finkok token authentication uses the token's username and the token value in
the service's password field. A read-only account check uses the demo
registration endpoint, SOAP action `get`, and fields `reseller_username`,
`reseller_password`, and a known demo issuer `taxpayer_id` (RFC):

`https://demo-facturacion.finkok.com/servicios/soap/registration`

The read-only check passed on 2026-09-05: Finkok returned the matching demo
issuer with active status and no error message. HTTP success alone must not be
interpreted as successful authentication; require a matching issuer record in
the response, and keep token and issuer data out of diagnostic output. No
stamping, cancellation, issuer registration, or production request was
performed. Revalidate after changing the token or issuer configuration.

The application integration and test CSD remain future work. Load these
credentials only into the fiscal service when that
module is implemented, with a separate credential set for any production use.

## Official references

- [Docker Engine on Ubuntu](https://docs.docker.com/engine/install/ubuntu/)
- [Docker firewall behavior](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
- [Docker local logging driver](https://docs.docker.com/engine/logging/drivers/local/)
- [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)
- [Caddy global options](https://caddyserver.com/docs/caddyfile/options)
- [Finkok token authentication](https://wiki.finkok.com/en/home/token)
- [Finkok read-only issuer lookup](https://wiki.finkok.com/home/webservices/registro_de_clientes/get)
