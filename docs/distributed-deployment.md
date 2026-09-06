# Grow from one server to several

The default installation keeps all roles on one Ubuntu 24.04 server. Add another
server when one role needs more CPU, memory, or a different network location.
The same release can run selected execution roles on different hosts. PostgreSQL
and Redis remain shared services; this is one application with distributed
execution, not separate databases for each business module.

On the first server, run:

```sh
curl -fsSL https://raw.githubusercontent.com/vothalvino/fireisp/main/install.sh | sudo bash
```

Choose **Main server** and accept billing, fiscal and network for the initial
single-server layout. Web, core events and the scheduler also run locally. The
wizard saves the selected placement in `COMPOSE_PROFILES`; later runs offer the
existing selection. These are execution modules: selecting a worker location
does not remove customers, plans or other business features from the application.

## Connect an additional server

Run the same command on the additional Ubuntu 24.04 server and choose
**Additional server**. Select one or several modules, give this server a unique
name, and enter the main server's SSH hostname/IP, port and administrator user.
Use root or an administrator with passwordless `sudo` for the enrollment helper.
You can supply an existing SSH private-key path or let OpenSSH request the
administrator password directly from the terminal. The installer does not save
that password. Subsequent enrollment changes may require administrator
authentication again; the persistent connection uses its own restricted key.

During first enrollment, verify the main server's SSH host-key fingerprint
against its console or another trusted record before accepting OpenSSH's prompt.
For an Ed25519 host key, obtain the fingerprint on the main server with
`sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. The accepted host identity
is pinned for the continuing connection.

The wizard transfers the required application settings over authenticated SSH;
there is no manual database-password copying. It creates a dedicated key limited
to forwarding the main server's loopback PostgreSQL, Valkey and private web
listeners. The main server's PostgreSQL administrator password is excluded. A
systemd service named `fireisp-link-NODE-ID.service` keeps the encrypted connection
running across restarts. Key material and connection settings are owner-only
under `/etc/fireisp/connections/NODE-ID`.

Prepare these connections before installation:

- The main server needs DNS pointing at its public IPv4 and inbound TCP 80/443
  for the application and certificate. Its SSH port must allow the additional
  server's source IP in the provider firewall and any host firewall.
- The additional server needs outbound access to the main server's SSH port and
  HTTPS access for source, packages and container images. Billing and fiscal
  workers do not need inbound 80/443 or their own public domain.
- The main server must already use the installer supporting enrollment and
  Docker Engine 28 or newer. PostgreSQL listens only on `127.0.0.1:15432`, Valkey
  on `127.0.0.1:16379`, and the private application on `127.0.0.1:18000`.
  These ports should remain unavailable from the public Internet.
- A network node also needs its own public IPv4 and the router-tunnel access
  required by the network provisioning workflow. Public RADIUS callbacks remain
  blocked; the node uses the private connection.

The additional server automatically downloads and builds the main server's
exact committed release. It starts only selected execution roles, verifies their
runtime heartbeats, and creates no database, administrator, migration or second
ISP account. For multiple modules, generated role IDs are `NODE-ID-billing`,
`NODE-ID-fiscal`, and so on. Rerunning the wizard installs the selected roles and
stops removed roles belonging to that same installation only after the selected
roles report healthy. Adding a remote worker does not automatically stop the
corresponding main-server worker; use the move procedure below after validation.

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
2. Prepare Ubuntu 24.04 on each additional server. The wizard installs Docker
   Engine and the Compose plugin when needed and configures the SSH connection.
   It checks database permissions, pending migrations, shared release and broker
   connectivity before starting roles. The database user must have no superuser,
   `CREATEDB` or `CREATEROLE` permissions.
3. Give each additional server a distinct lowercase name. A network node also
   needs its own public IPv4 router-tunnel endpoint. Enrollment registers that
   node and its token; it does not relocate an already provisioned router.

For an existing operator-managed private network, the lower-level node installer
also accepts an owner-only environment file and private service URLs, as shown
below. Use `deploy/nodes/environment.example` and provide the shared application
keys and application database credentials without the PostgreSQL administrator
password. This manual path is optional; the interactive wizard supplies its
settings through SSH automatically.

Shared application keys let a worker read encrypted application records. This
deployment isolates execution resources and privileged network components; it
does not give every business role an independent database security boundary.

The scheduler checks its exclusive PostgreSQL session before starting and every
five seconds while running. Losing that connection stops its child, but a failure
can still produce duplicate queue deliveries during detection or broker recovery.
Job claims and idempotent business operations remain necessary; scheduler
leadership is not an exactly-once delivery guarantee.

## Advanced: install a role directly

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

The interactive wizard already establishes and supervises its restricted SSH
tunnel. If using the advanced direct installer with your own SSH tunnels,
establish and supervise them separately using a dedicated SSH
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
2. Run the one-line wizard on another server, choose **Additional server**, and
   select the role. Enrollment obtains the matching release and shared settings.
3. Confirm the new node's fresh heartbeat and correct release. Submit a demo job
   for its queue and confirm the result from the existing web application. For
   fiscal work, confirm XML/PDF retrieval from a different web instance. For a
   network node, verify its assigned router, RADIUS authentication, and accounting.
4. Gracefully stop the old worker for that role after its active jobs finish.
   Do not use `kill -9` to transfer fiscal work: an interrupted external request
   can have an uncertain provider result requiring recovery. Keep core and billing
   workers running while moving fiscal work, and keep one active scheduler.
   On the main server, rerun the wizard at the same release and deselect the moved
   role, or persist the local placement with the main installer's `--local-workers`
   option: `--local-workers billing,network` removes local fiscal execution;
   `--local-workers billing,fiscal,network` restores all three local roles.
   Use the existing hostname, public IP and same release/source arguments. A
   normal installer rerun without this option preserves the saved selection.
5. Watch queue backlog, processing latency, failures, database connections, memory,
   and node heartbeat freshness before moving another role.

When changing only placement, pin the wizard to the main server's current full
Git SHA so a newer repository `main` does not turn the change into an upgrade:

```sh
sudo docker compose --project-directory /opt/fireisp/staging exec -T web python manage.py shell -c "from django.conf import settings; print(settings.FIREISP_RELEASE)"
curl -fsSL https://raw.githubusercontent.com/vothalvino/fireisp/main/install.sh | sudo bash -s -- --release FULL_GIT_SHA
```

Replace `FULL_GIT_SHA` with the release printed by the first command. A placement
rerun can keep remote workers active only when the declared release matches and
there are no pending migrations. A release or schema change still requires the
coordinated remote drain described above.

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

Selecting a new network module registers a new node but leaves existing routers
on their current node. Plan the router endpoint change, drain its jobs, replay
accounting, and validate authentication and accounting on the destination before
retiring its old network processes. Module installation alone is not that cutover.

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
