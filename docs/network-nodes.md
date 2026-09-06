# Network server placement

FireISP starts with the web application, workers and `primary` network node on one
server. `NetworkNode` gives each network server its own router assignment, public
endpoint, RADIUS callback token, local Unix agent socket, confirmed authorization
snapshot and accounting journal. Existing routers migrate to `primary` without
changing any NAS configuration, addresses, WireGuard keys or RADIUS secrets.

The network role remains a trusted part of the same deployment: nodes use the
same release, PostgreSQL database and application encryption key. Assignment is
an execution boundary, not database tenant isolation. RADIUS HTTP tokens are
separate: the API identifies a node from its token and accepts authorization and
accounting only for NAS addresses assigned to that node. An arbitrary node header
cannot enlarge that scope. The legacy callback token works only for `primary`,
and stops working if a registered primary token replaces it.

## Adding capacity for new routers

1. Install the same release on a server with the network role, local network
   agent and FreeRADIUS. Give it a distinct `NETWORK_NODE_ID` such as `north` and
   a distinct process `FIREISP_NODE_ID`. PostgreSQL connectivity and accounting
   callbacks must use the private deployment transport; the root agent keeps its
   local Unix socket and has no remote HTTP listener.
2. Generate a unique callback token in a protected file. On the control plane,
   register the node using secret standard input:
   `python manage.py register_network_node north --endpoint 198.51.100.23 --radius-token-stdin < /protected/node-token.txt`.
   Configure that token for the node's accounting callback. This command stores a
   digest and never prints the token. Endpoint edits are refused while existing
   provisioned routers depend on that endpoint.
3. Start `python manage.py run_network_jobs` with `NETWORK_NODE_ID=north` and the
   deployment's matching release identifier. The node must be registered before
   it starts. The agent, RADIUS state directory and worker socket belong to this
   server and must not be shared with another network node.
4. Select **Servidor de red** when creating a new router. Its plan shows that
   node's registered public endpoint. Complete the existing SSH trust, reviewed
   provisioning and actual PPPoE/accounting tests on the assigned node.
5. Check `network_sync:north` and the runtime network heartbeat. Primary keeps
   the existing `network_sync` code. Healthy primary status does not demonstrate
   a remote node's health.

The registered endpoint supplies future reviewed plans. Changing application
server placement does not automatically move WireGuard endpoints or NAS state.

## Moving an existing network workload

A provisioned router cannot be moved by changing a dropdown or by launching a
second worker with the same node ID. Moving it changes the peer endpoint and
requires a maintenance cutover plan. Keep its old node and configuration until
rollback is verified. Inventory the affected routers, WireGuard keys, local
RADIUS cache, accounting journal/cursor, firewall rules and persistent agent
state; prepare the destination privately; drain the source worker and accounting
replay; stop the source agent/RADIUS before transferring state. The old and new
host must never advertise or write the same node concurrently. Preserve NAS
configuration and a tested return route to the source. Only after a reviewed NAS
endpoint cutover and successful real PPPoE, rate, disconnect and Start/Stop tests
should the endpoint registry and host placement be finalized. FireISP does not
currently automate this existing-router migration. Creating an additional node
for new routers is the supported incremental expansion path.

## Ownership, concurrency and interrupted effects

Distinct nodes run independently. Each node intentionally has **one active
executor** for provisioning and complete cache publication. PostgreSQL session
advisory locks serialize multiple worker processes pointing at that node; a
standby process skips work while another process owns it. A durable token,
generation and 300-second renewable lease fence a process after a lost database
connection or replaced ownership. SQLite is for local development and does not
provide the PostgreSQL concurrent-worker guarantee. Do not use transaction-mode
connection pooling for this session-lock worker.

Ownership is checked before and after local agent calls and SSH commands.
PostgreSQL cannot cancel a command that already reached a NAS when a connection
fails; this is not an exactly-once execution guarantee. An interrupted running
job is marked failed and its router quarantined after the replacement acquires
ownership. Other pending jobs for that router stay stopped until an operator
reviews the NAS and agent and explicitly retries the failed job. Unexpired
leases cannot be stolen merely by passing `--recover-stale`; that option no
longer blindly returns interrupted jobs to pending. Existing owned-resource
journals remain available for recovery or rollback.

The worker handles SIGTERM/SIGINT by finishing current work before exiting.
A hard kill, an expired lease or an interrupted transport requires the same
review. Cached confirmed RADIUS authorization and the local accounting journal
continue to provide the existing bounded control-plane-outage behavior.
