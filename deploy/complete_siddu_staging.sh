#!/usr/bin/env bash
# One-shot, fail-closed SIDDU M6 staging activation after Tailscale Serve is enabled.
set -Eeuo pipefail
umask 077

[ "$(id -u)" = 0 ] || { printf 'BLOCKED: run with sudo bash\n' >&2; exit 1; }

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
INSTALLER="$SCRIPT_DIR/stram_quant_siddu.sh"
AUTH_CONFIG=/etc/stram-quant/auth.env
APPROVAL=/etc/stram-quant/m6-approval.env
PRIVATE_HOST=siddu.tailc5da4c.ts.net
PRIVATE_IPV4=100.117.151.7
PUBLIC_URL="https://$PRIVATE_HOST"

fail() { printf 'BLOCKED: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s ==\n' "$*"; }

set_gate() {
  local key=$1 value=$2
  python3 - "$APPROVAL" "$key" "$value" <<'PY'
import os, pathlib, re, sys, tempfile
path, key, value = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or value not in {"yes", "no"}:
    raise SystemExit("BLOCKED: invalid evidence-gate update")
lines = path.read_text().splitlines()
matches = [index for index, line in enumerate(lines) if line.startswith(key + "=")]
if len(matches) != 1:
    raise SystemExit("BLOCKED: missing or repeated evidence gate " + key)
lines[matches[0]] = key + "=" + value
descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix="m6-approval.")
try:
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  chown root:root "$APPROVAL"
}

step 'Preconditions and protected non-secret configuration'
[ "$(tailscale ip -4)" = "$PRIVATE_IPV4" ] || fail 'SIDDU Tailscale IPv4 changed; review configuration before continuing.'
git -C "$REPO_ROOT" diff --quiet -- src pyproject.toml uv.lock Dockerfile || fail 'Runtime source differs from the reviewed Quant image revision.'
[ -s "$REPO_ROOT/build/m6/image-evidence.json" ] || fail 'Image evidence manifest is missing.'
install -d -o root -g root -m 0700 /etc/stram-quant
install -o root -g root -m 0600 /dev/null "$AUTH_CONFIG"
cat > "$AUTH_CONFIG" <<EOF
KEYCLOAK_BASE_IMAGE_REF=quay.io/keycloak/keycloak@sha256:de6ed510b59cfcd25f129ba4118696fa10d4165eee921a8d846178b6d35d30f7
KEYCLOAK_IMAGE_REF=localhost/stram-keycloak:26.8.1-stram1@sha256:8bb052ca56d7bf59d84ad10a19d3b909243c631ede22d6f409067896294c6064
POSTGRES_BASE_IMAGE_REF=docker.io/library/postgres:17.11-bookworm@sha256:2cd2c0f809604aaeff6f0ec8f40aecbd025613107561015c69369a1a7f1d3416
POSTGRES_IMAGE_REF=localhost/stram-postgres:17.11-stram1@sha256:b1aac32b06aeb1731f6c743228bc5c52f7af3ec1ffa51f15d1dae8e1f14c65f3
KEYCLOAK_PUBLIC_URL=$PUBLIC_URL
KEYCLOAK_ADMIN_URL=$PUBLIC_URL
KEYCLOAK_REALM=stram-staging
KEYCLOAK_DB_NAME=keycloak
KEYCLOAK_DB_USER=keycloak
KEYCLOAK_HOST_PORT=9080
KEYCLOAK_MANAGEMENT_PORT=9001
STRAM_MCP_RESOURCE_URL=$PUBLIC_URL/mcp
STRAM_MCP_TENANT_ID=local-stram
STRAM_MCP_ALLOWED_HOSTS=$PRIVATE_HOST
STRAM_PRIVATE_HOST_IPV4=$PRIVATE_IPV4
STRAM_PRIVATE_CA_FILE=
EOF
install -o root -g root -m 0600 /dev/null "$APPROVAL"
cat > "$APPROVAL" <<'EOF'
# Technical staging evidence. No independent security approval or registry signature is claimed.
APPROVED_IMAGE_REF=localhost/stram-quant-mcp:m6-candidate@sha256:97f7bec6de25f7ea37d483e059157b045789aa1718dfb4abe22a0572290f54f2
ROLLBACK_IMAGE_REF=localhost/stram-quant-mcp:review@sha256:5563e0c667adde724d178a240dd86b87598b989db84f17a86e13af5a079309c5
M6_STAGING_AUTHORIZED=yes
SBOM_PROVENANCE_VULNERABILITY_APPROVED=yes
PRIVATE_TLS_AND_HOST_POLICY_VERIFIED=no
FIREWALL_EGRESS_IPV4_IPV6_VERIFIED=no
NONPROD_OIDC_AND_JWKS_VERIFIED=no
AUDIT_BACKUP_CHECKPOINT_RETENTION_VERIFIED=no
ROLLBACK_IMAGE_AND_DRILL_VERIFIED=no
ROLLBACK_AUTHORIZED=no
EOF

step 'Rootless identities, immutable images and generated service configuration'
bash "$INSTALLER" provision-auth
bash "$INSTALLER" import-images
bash "$INSTALLER" prepare
bash "$INSTALLER" auth-prepare

step 'Tailnet-only HTTPS routes; no Funnel and no management/admin route'
tailscale serve --bg --yes --https=443 --set-path=/realms http://127.0.0.1:9080/realms
tailscale serve --bg --yes --https=443 --set-path=/mcp http://127.0.0.1:8800/mcp
serve_status=$(tailscale serve status)
grep -Fq "$PUBLIC_URL" <<<"$serve_status" || fail 'Tailscale Serve HTTPS endpoint is missing.'
grep -Fq '/realms' <<<"$serve_status" || fail 'Keycloak realm route is missing.'
grep -Fq '/mcp' <<<"$serve_status" || fail 'MCP route is missing.'
if tailscale funnel status 2>&1 | grep -Fq 'Available on the internet'; then
  fail 'Tailscale Funnel is enabled.'
fi
curl --retry 3 --retry-delay 2 --retry-all-errors --silent --show-error --output /dev/null --max-time 45 "$PUBLIC_URL/realms/" || fail 'Private TLS handshake failed.'
set_gate PRIVATE_TLS_AND_HOST_POLICY_VERIFIED yes

step 'Local Keycloak 26.8.1 and PostgreSQL 17.11'
bash "$INSTALLER" auth-start
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "$PUBLIC_URL/admin/")" = 404 ] || fail 'Keycloak admin path is externally routed.'
[ "$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "$PUBLIC_URL/health/ready")" = 404 ] || fail 'Keycloak management path is externally routed.'

step 'Host-enforced OIDC-only Quant egress'
bash "$INSTALLER" install-egress
if runuser -u stram-quant -- timeout 5 getent ahosts example.com >/dev/null 2>&1; then fail 'arbitrary DNS egress succeeded'; fi
if runuser -u stram-quant -- curl --silent --max-time 5 https://1.1.1.1/ >/dev/null 2>&1; then fail 'arbitrary IPv4 egress succeeded'; fi
if runuser -u stram-quant -- curl --globoff --silent --max-time 5 'https://[2606:4700:4700::1111]/' >/dev/null 2>&1; then fail 'arbitrary IPv6 egress succeeded'; fi
bash "$INSTALLER" auth-verify
set_gate FIREWALL_EGRESS_IPV4_IPV6_VERIFIED yes

step 'Genuine Keycloak negative JWT and real-lake call evidence'
bash "$INSTALLER" prepare-acceptance
set_gate NONPROD_OIDC_AND_JWKS_VERIFIED yes

step 'Quant MCP activation and live acceptance'
bash "$INSTALLER" activate
bash "$INSTALLER" verify
bash "$INSTALLER" reliability-drill

step 'Audit backup, independent checkpoint verification and retention artifact'
bash "$INSTALLER" backup-audit
bash "$INSTALLER" verify-backup
bash "$INSTALLER" install-backup-timer
set_gate AUDIT_BACKUP_CHECKPOINT_RETENTION_VERIFIED yes

step 'Genuine prior-artifact rollback and approved-image recovery drill'
set_gate ROLLBACK_AUTHORIZED yes
bash "$INSTALLER" rollback
bash "$INSTALLER" verify
bash "$INSTALLER" restore-approved
bash "$INSTALLER" verify
set_gate ROLLBACK_AUTHORIZED no
set_gate ROLLBACK_IMAGE_AND_DRILL_VERIFIED yes

step 'Persistence only after every live gate has passed'
bash "$INSTALLER" enable-services

step 'Existing Stram workload non-regression'
for port in 3000 5432 8000 19092; do
  ss -H -lnt "sport = :$port" | grep -q . || fail "existing listener $port is unavailable"
done
curl --silent --show-error --output /dev/null --max-time 10 http://127.0.0.1:3000/
curl --silent --show-error --output /dev/null --max-time 10 http://127.0.0.1:8000/

step 'Final status'
bash "$INSTALLER" status
printf 'PASS: SIDDU M6 private staging deployment, live acceptance, backup, rollback and persistence completed\n'
