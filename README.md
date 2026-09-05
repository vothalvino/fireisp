# FireISP

Modular management software planned for a fixed-wireless ISP in Cuauhtémoc,
Chihuahua, Mexico.

## Current status

The repository currently provides the VPS staging infrastructure. The ISP
application is not implemented yet. The staging health endpoint reports
`application_ready: false`; it only checks the infrastructure response.

See [deployment and recovery instructions](docs/deployment.md) for the Docker
Compose configuration, HTTPS setup, validation, and operational boundaries.

## Planned application

- Python/Django with a Spanish interface and separate business modules.
- Customer registration, contracts, installations, support, and wireless inventory.
- Monthly advance payments, cash collection, and bank-transfer reconciliation.
- Finkok integration for Mexican fiscal documents.
- MikroTik PPPoE access with FreeRADIUS.

## License

FireISP source is licensed under the [MIT License](LICENSE). Third-party
software retains its own license.
