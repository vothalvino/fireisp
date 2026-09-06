# HTTPS runtime security rebuild

`deploy/staging/Dockerfile.caddy` rebuilds the existing Caddy 2.11.4 release
with Go 1.26.8. The previously deployed official Caddy image contained Go
1.26.3. That runtime is affected by the TLS KeyUpdate resource-exhaustion
vulnerability [GO-2026-6090 / CVE-2026-56862](https://pkg.go.dev/vuln/GO-2026-6090).
The Go advisory fixes the 1.26 release family in 1.26.6; the selected 1.26.8
patch release is listed in the [official Go downloads](https://go.dev/dl/).

The build preserves the Caddy release and its upstream dependency versions.
It downloads the source by its full commit, checks its archive SHA256, checks
the upstream `go.mod` and `go.sum`, builds with `-mod=readonly`, verifies the
downloaded modules, and checks that both module files remain unchanged.
`GOTOOLCHAIN=local` prevents an implicit compiler download. The build fails
unless the actual compiler and the resulting binary report Go 1.26.8.

## Pinned inputs

These inputs were verified against upstream source and Docker's official
image registry during the September 2026 fix. Image digests below identify
multi-platform indexes; Docker chooses the corresponding target platform.

| Input | Version and immutable reference |
| --- | --- |
| Go builder | `golang:1.26.8-alpine@sha256:ce864e7223ac17b1775e6fd0b4c0db580c2eb50e7953a427916379e4b92a1628` |
| Runtime | `alpine:3.24@sha256:28bd5fe8b56d1bd048e5babf5b10710ebe0bae67db86916198a6eec434943f8b` (Alpine 3.24.1) |
| Caddy source | [v2.11.4 commit `e2eee6a7fce366321294c9c2a79f3146891dcbdf`](https://github.com/caddyserver/caddy/tree/e2eee6a7fce366321294c9c2a79f3146891dcbdf) |
| Caddy source archive SHA256 | `a593bd7077c76102ca76d19287a5e247d4e359dd67eddbc933f865afd3c131eb` |
| Upstream `go.mod` SHA256 | `2ce7537aecbdaf5b2fe42bf0cc42f2d7e7ce5e4186aab3586b736a53f5937abd` |
| Upstream `go.sum` SHA256 | `d4e2d1812e7e38d24f947adf93afddb9b04b2bc8102fefc3e5b43fc2540dea79` |

The [official Go builder definition](https://github.com/docker-library/golang/blob/f47489bcbda87966b421340c536f39a34d00b45f/1.26/alpine3.24/Dockerfile)
records its compiler download checksums and signature verification. The
[Go module proxy release record](https://proxy.golang.org/github.com/caddyserver/caddy/v2/@v/v2.11.4.info)
independently identifies the Caddy tag's source commit.

The final image copies only the compiled Caddy executable, its license, and
the CA trust bundle from the pinned builder into the minimal Alpine runtime.
Caddy's `/data`, `/config`, `/usr/bin/caddy`, default command and `/srv` working
directory remain compatible with the existing deployment. The repository's
Caddyfile is included; Compose mounts the installed configuration over it.
No Go compiler or previous Caddy executable is included in the runtime.

## Verification and future updates

Build and inspect the candidate before recreating the HTTPS container:

```bash
cd /opt/fireisp/staging
sudo docker compose build caddy
sudo docker compose run --rm --no-deps caddy caddy build-info \
  | python3 /opt/fireisp/app/deploy/check_caddy.py
sudo docker compose run --rm --no-deps caddy caddy version
sudo docker compose run --rm --no-deps caddy caddy validate \
  --config /etc/caddy/Caddyfile --adapter caddyfile
```

The build information must identify Go 1.26.8 and `caddy version` must identify
`v2.11.4-fireisp1`. Building the main module directly from its verified source
archive records its Go module version as `(devel)`; the downstream Caddy version
is supplied through Caddy's supported `CustomVersion` linker variable. The
source commit is also recorded in the image's provenance label.

Scan the resulting image with a current vulnerability database and retain the
report with the release evidence. The actual Go version establishes that the
specified TLS vulnerability is fixed; it does not establish that the entire
image has no other advisories. After replacement, check public HTTPS, the HTTP
redirect, application health, and the running container's build information.
No denial-of-service exploit is required for those checks.

For future updates, review both Go's supported releases and Caddy's current
release. Update the image digests and source hashes deliberately, build and
scan again, and preserve the previously verified image for rollback. A moving
`caddy:2-alpine` tag alone does not prove its embedded Go runtime is patched.
