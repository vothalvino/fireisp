# Software-managed installation and onboarding

FireISP must perform its repeatable setup through its installer and admin
interface. Manual SSH operations are for initial access, diagnostics, and
recovery. They must not become a requirement for configuring each ISP or router.

## Installer responsibilities

- Check Ubuntu 24.04, disk space, memory and hostname DNS before changing the
  host. Require the A record to match the selected VPS and identify conflicting
  AAAA records when observed.
- Install and configure the application runtime, HTTPS, persistent storage, and
  a restricted provisioning worker where host-level changes are required.
- Initialize the database and provide a secure first-administrator setup flow.
- Support repeatable upgrades, backups, restore checks, and health diagnostics.
- Explain the exact provider firewall changes when no provider API is connected;
  distinguish those external prerequisites from settings the installer controls.
- Verify the deployed HTTPS `/healthz` endpoint with normal certificate and
  hostname validation, bounded retries, and explicit application/database
  readiness. A maintenance page or HTTP redirect does not complete installation.

`deploy/install.py` now reproduces Docker, the application stack, HTTPS,
database migrations, initial account setup and scheduled encrypted backups.
Reruns preserve authentication secrets and update the inspected source and
public hostname settings. Provider DNS/firewall changes remain external
prerequisites; an HTTPS check originating on the VPS does not establish
reachability from every outside network.

## Admin onboarding responsibilities

1. Configure the ISP, branches, operating timezone, plans, and billing rules.
2. Configure Finkok demo credentials and issuer RFC in protected settings; run
   the read-only issuer check. Keep demo and production configuration separate.
3. Add a router with its management address, credentials, and lab/production
   designation. Discover RouterOS version, account capabilities, interfaces,
   routes, existing tunnels/RADIUS settings, and active subscriber sessions.
4. Present the proposed connection and configuration changes. Apply only
   FireISP-managed resources; retain unrelated settings. Reusing existing
   resources or changing global PPP/RADIUS behavior requires an explicit choice.
5. Create the private connection, generate and exchange keys, and verify traffic
   through the tunnel. Report provider firewall prerequisites precisely.
6. Configure the selected subscriber access and RADIUS integration, then run
   controlled tests using isolated test users and a real lab PPPoE client.
7. Display the results and outstanding prerequisites before marking a feature
   ready for use.

The web application must not run as root or expose a general-purpose remote
shell. A restricted worker performs predefined provisioning operations with
validated inputs. Store secrets outside source control and public responses;
give only the relevant worker access to them. Do not expose the Docker socket
or unrestricted sudo to the web application. Account/key management that needs
extra RouterOS privileges must be separate from routine provisioning.

Provisioning operations must track intent, job status, affected resources, and
observed results. Retries must not duplicate resources. Save the pre-change
state needed for recovery, tag managed resources, and limit rollback to the
changes made by the operation. Do not reset a router or replace its default
route to connect it to FireISP.

## Acceptance criteria

- A new installation can reach the admin onboarding flow using documented
  installer commands, without hand-editing router or application configuration.
- Adding the same router twice does not create duplicate tunnels or RADIUS
  entries, and existing unrelated configuration is preserved.
- Insufficient permissions and blocked ports produce actionable diagnostics.
- A failed or interrupted job can be retried or rolled back without losing
  management access or altering unrelated resources.
- SSH login, a tunnel handshake, and a RADIUS server response are reported as
  separate results. Subscriber authentication, accounting, speed changes, and
  disconnect/reconnect are successful only after actual PPPoE session tests.
- A routed WireGuard tunnel alone is not reported as an Ethernet/PPPoE lab;
  the test runner verifies that an isolated layer-2 client path exists.
- Finkok credential validation is separate from successful demo fiscal-document
  issuance/cancellation and never enables production operations implicitly.

## Current implementation and lab verification

The application now provides router registration, pinned SSH identity,
discovery, reviewed provisioning jobs, a private WireGuard/EoIP lab path and
real temporary PPPoE session tests. These are application workflows, not
manual commands required for every router. Existing unrelated resources are
preserved, and rollback operates on the recorded resources owned by FireISP.

Consult the [network module and its limits](network.md) and the
[current verification record](testing.md) for the latest measured results.
The application also retains each provisioning job and its observed evidence.
Successful SSH access, a tunnel, a configured speed queue, actual throughput
and Internet reachability are different checks; the interface must preserve
those distinctions. The isolated lab does not authorize migrating existing
production subscriber interfaces.

## Technical references

- [RouterOS permissions](https://help.mikrotik.com/docs/spaces/ROS/pages/8978504/User)
- [RouterOS WireGuard](https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/)
- [RouterOS PPPoE](https://manual.mikrotik.com/docs/virtual-private-networks/pppoe/)
