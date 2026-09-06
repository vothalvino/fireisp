# Grow from one server to several

The default installation keeps all roles on one Ubuntu 24.04 server. Add another
server when one role needs more CPU, memory, or a different network location.
The same release can run selected execution roles on different hosts. PostgreSQL
and Redis remain shared services; this is one application with distributed
execution, not separate databases for each business module.

The main installer writes `COMPOSE_PROFILES=billing,fiscal,network` on the first
installation and preserves it on subsequent runs. Web, core events and the
scheduler also run locally. No second server, registry or private network is
required for this initial layout.

| Execution role | Work performed | How it grows |
| --- | --- | --- |
| `web` | Staff/customer requests and API endpoints | Additional web hosts behind a private reverse proxy or load balancer |
| `worker` | Core events and coordination | Additional workers consuming the `core` queue |
| `billing` | Billing jobs | Dedicated workers consuming the `billing` queue |
| `fiscal` | Finkok and document jobs | Dedicated workers consuming the `fiscal` queue |
| `scheduler` | Periodic job publication | One active PostgreSQL-lock-protected scheduler; extra schedulers stand by |
| `network` | Jobs for one assigned network node, its privileged local agent, and RADIUS | Additional node IDs with explicitly assigned routers |

Moving a worker does not require moving the customer database or installing
another independent ISP account. Multiple workers may consume a supported queue.
A network node owns its local tunnel interfaces, agent socket, RADIUS generated
configuration, and accounting journal. Those files do not belong on web workers.

## Prerequisites

1. Before an upgrade, stop and drain executors on **every remote node**, then
   deploy one common Git release and apply its database migrations on the control
   server. The primary installer drains local executors and rejects recently active
   remote nodes; it does not orchestrate remote shutdown. A stale heartbeat is not
   proof a disconnected worker has stopped. Initialize/update the shared release with
   `python manage.py set_deployment_release --release FULL_GIT_SHA` as part of the
   controlled deployment. Do not change the shared release just to bypass a
   mismatched image. Nodes validate it before starting their work.
2. Prepare Ubuntu 24.04 and Docker Engine with the Compose plugin on each new
   server. The node installer deliberately does not initialize databases, create
   administrator accounts, run migrations, or expose database/cache ports.
3. Provide private access to the existing PostgreSQL and Redis. Use an encrypted
   private network, verified service TLS, or operator-managed SSH tunnels with
   pinned host keys. Restrict access to the intended node IPs. The validator
   requires private endpoint addresses; it rejects public addresses even with TLS.
4. Copy the existing `SECRET_KEY` and `ENCRYPTION_KEY`, the application database
   URL, Redis URL, and host/origin settings into an owner-only environment file
   outside Git. Start with `deploy/nodes/environment.example`. Do not copy the
   PostgreSQL administrator password. The installer forwards only an explicit
   role-specific allowlist and checks that the connected database user has no
   superuser, `CREATEDB`, or `CREATEROLE` permissions.
5. Give each execution instance a distinct lowercase node ID, such as `fiscal-1`.
   A network node additionally needs its registered token, its own public IPv4
   router-tunnel endpoint, and a reachable **private** RADIUS callback URL. Public
   HTTPS blocks `/network/radius`; it is not a substitute for private connectivity.

Shared application keys let a worker read encrypted application records. This
deployment isolates execution resources and privileged network components; it
does not give every business role an independent database security boundary.

The scheduler checks its exclusive PostgreSQL session before starting and every
five seconds while running. Losing that connection stops its child, but a failure
can still produce duplicate queue deliveries during detection or broker recovery.
Job claims and idempotent business operations remain necessary; scheduler
leadership is not an exactly-once delivery guarantee.

## Install a role

These commands assume an inspected checkout contains the same committed release
as the control server. Replace the example full SHA with that release. The file
must have mode `600`; secrets are read from it rather than command arguments.

```sh
sudo chmod 600 /etc/fireisp/fiscal-node.env
sudo python3 deploy/nodes/install.py \
  --role fiscal --node-id fiscal-1 \
  --env-file /etc/fireisp/fiscal-node.env \
  --release FULL_40_CHARACTER_GIT_SHA \
  --check-only
```

`--check-only` validates endpoint addresses, required keys, node identity, and the
role configuration without installing anything. It prints a redacted summary.
It does not claim that the remote services or image are ready.

Remove `--check-only` to build and start the role. The installer builds from a
`git archive` of the exact commit, so working-tree changes and untracked private
files do not enter the image. It tags and verifies the image's OCI revision label,
then checks database permissions, migrations, the shared release, and Redis before
starting services. A container starting is not evidence that a routed business
job has completed; verify the runtime heartbeat and a representative job next.

Use `--concurrency 2` to change worker concurrency. The `web` role uses that value
for Gunicorn process count. Choose it based on measured memory and database
connection capacity, not just CPU count.

You can also supply an image already built from that release:

```sh
sudo python3 deploy/nodes/install.py \
  --role billing --node-id billing-1 \
  --env-file /etc/fireisp/billing-node.env \
  --release FULL_40_CHARACTER_GIT_SHA \
  --image your-registry/fireisp@sha256:YOUR_VERIFIED_IMAGE_DIGEST
```

Add `--pull` if that image is in your registry but is not present locally. No
registry is assumed or provisioned. The matching
`org.opencontainers.image.revision` label is required even when an image digest
is supplied. A network installation using existing images also requires matching
`--agent-image` and `--radius-image` values; otherwise the installer builds all
three images from the declared commit.

Role configuration is stored at `/opt/fireisp/nodes/NODE-ID/compose.json`, inside
a mode `700` directory, with the file at mode `600`. It contains runtime secrets.
Use `docker compose ... config --quiet` for syntax validation; plain `config`
prints resolved secrets. A rerun preserves the prior configuration as
`compose.previous.json` before replacement.

## Private connectivity

The generated services use the host network so existing VPN routes and loopback
SSH tunnels are reachable. Application containers remain unprivileged with all
capabilities dropped. Only the network agent receives the capabilities and device
access needed to create its explicitly managed interfaces.

For direct private connectivity, use private IPs or DNS names that resolve only
to private IPs. PostgreSQL TLS URLs can use `sslmode=verify-full`; Redis TLS uses
`rediss://` with certificate verification required. If certificates require
additional files, pass `--ca-directory /etc/fireisp/certificates`; its contents are
mounted read-only at `/run/fireisp-certs`. Set the service URL's supported CA-file
option to the mounted path. Make CA files readable by container UID 1000; do not
put unrelated credentials in this directory.

For SSH tunnels, establish and supervise them separately using a dedicated SSH
key, verified `known_hosts`, strict host-key checking, and
`ExitOnForwardFailure=yes`. Restrict the remote account/key to the intended
forwarding destinations. Bind each local forwarding listener to `127.0.0.1`.
The control server's destination listeners must themselves be private/loopback;
do not publish PostgreSQL or Redis to `0.0.0.0` to make forwarding work. Use those
local forwarded ports in `DATABASE_URL`, `REDIS_URL`, and the RADIUS callback URL,
then add `--allow-loopback-tunnels` to the installer. A tunnel failure fails
preflight or prevents work; it does not silently select a local database.

Web instances bind to `127.0.0.1:18000` by default. `--web-port` changes only the
port, not the loopback bind. Put the HTTPS reverse proxy on that host or forward
over the private network to its loopback listener. The node installer does not
create certificates or open a public listener. RADIUS listens only on the private
addresses generated by the reviewed network provisioning workflow.

## Move one role safely

1. Start with the default single-server installation and take a verified backup.
2. Add one remote role using the same release, keys, and shared services.
3. Confirm the new node's fresh heartbeat and correct release. Submit a demo job
   for its queue and confirm the result from the existing web application. For
   fiscal work, confirm XML/PDF retrieval from a different web instance. For a
   network node, verify its assigned router, RADIUS authentication, and accounting.
4. Gracefully stop the old worker for that role after its active jobs finish.
   Do not use `kill -9` to transfer fiscal work: an interrupted external request
   can have an uncertain provider result requiring recovery. Keep core and billing
   workers running while moving fiscal work, and keep one active scheduler.
   Persist the local placement with the main installer's `--local-workers`
   option: `--local-workers billing,network` removes local fiscal execution;
   `--local-workers billing,fiscal,network` restores all three local roles.
   Use the existing hostname, public IP and same release/source arguments. A
   normal installer rerun without this option preserves the saved selection.
5. Watch queue backlog, processing latency, failures, database connections, memory,
   and node heartbeat freshness before moving another role.

For rollback within the same release, stop the remote role gracefully and restart
the previous role on the control server. Shared durable jobs remain in the broker
and database. If the issue is node configuration, restore its protected
`compose.previous.json` and run Compose against it. Changing the cluster release
or reverting a database migration requires a coordinated release rollback; an
old worker must not be started against a newer declared release.

Moving a router between network nodes is an explicit provisioning change. Keep
its old local accounting journal until all records have replayed and its private
interfaces have been cleaned up. Do not copy agent sockets or point two active
node IDs at the same router as a shortcut.

## Remaining shared infrastructure

Adding workers increases execution capacity but leaves PostgreSQL and Redis as
shared dependencies. Plan PostgreSQL connection pooling, replication and tested
restores, broker persistence/recovery, HTTPS load balancing, and monitoring as
separate infrastructure work when measured load or availability requirements
justify them. This installer does not claim automatic database failover or that
a particular subscriber count can be sustained without workload testing.

Fiscal artifacts use the shared application storage implementation rather than
requiring a worker-local documents volume. Preserve legacy filesystem artifacts
until their migration has been verified. Back up the database centrally and the
network nodes' local state/accounting journals separately; a backup of the first
server alone cannot include files living on another host.
