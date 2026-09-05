# Software-managed installation and onboarding

FireISP must perform its repeatable setup through its installer and admin
interface. Manual SSH operations are for initial access, diagnostics, and
recovery. They must not become a requirement for configuring each ISP or router.

## Installer responsibilities

- Check the host operating system, available resources, DNS, and required ports.
- Install and configure the application runtime, HTTPS, persistent storage, and
  a restricted provisioning worker where host-level changes are required.
- Initialize the database and provide a secure first-administrator setup flow.
- Support repeatable upgrades, backups, restore checks, and health diagnostics.
- Explain the exact provider firewall changes when no provider API is connected;
  distinguish those external prerequisites from settings the installer controls.

Docker and HTTPS on the current VPS are bootstrap infrastructure. Future
installations must reproduce them through the installer rather than repeated
manual commands.

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

## Current lab state (2026-09-05)

The CHR was inspected read-only. It runs RouterOS 7.21.5, has no active PPP
sessions, and already contains an older test tunnel and RADIUS configuration.
Those settings were not changed. The supplied account is in the `write` group;
the software must discover its capabilities rather than assume administrator
permissions.

The VPS can reach the CHR's SSH port after the provider firewall adjustment.
WireGuard tools were installed on the VPS, but no tunnel was configured or
activated. Unused keys generated during preparation were removed. Router
onboarding and private-link provisioning remain application work.

## Technical references

- [RouterOS permissions](https://help.mikrotik.com/docs/spaces/ROS/pages/8978504/User)
- [RouterOS WireGuard](https://manual.mikrotik.com/docs/virtual-private-networks/wireguard/)
- [RouterOS PPPoE](https://manual.mikrotik.com/docs/virtual-private-networks/pppoe/)
