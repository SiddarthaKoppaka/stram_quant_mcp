#!/usr/bin/env bash
# Consolidated SIDDU private-staging workflow. It never auto-approves or boot-enables services.
set -Eeuo pipefail
umask 077

ACTION=${1:-doctor}
QUANT_USER=stram-quant
QUANT_HOME=/home/stram-quant
AUTH_USER=stram-auth
AUTH_HOME=/home/stram-auth
LAKE=/srv/stram-quant/lake
AUDIT_VOLUME=stram-quant-mcp-audit
AUDIT_SECRET=stram-quant-audit-key
DATA_SECRET=stram-quant-data-client-secret
DATA_SERVICE_PORT=${STRAM_QUANT_DATA_PORT:-8444}
DATA_CLIENT_ID=${STRAM_QUANT_DATA_CLIENT_ID:-stram-data-mcp-staging}
DATA_SECRET_TARGET=data-client-secret
REVIEW_IMAGE=localhost/stram-quant-mcp:review
QUANT_NETWORK=stram-quant-private
AUTH_NETWORK=stram-auth-private
CONFIG_DIR="$QUANT_HOME/.config/stram-quant-mcp"
SERVICE_CONFIG="$CONFIG_DIR/service.env"
APPROVAL=/etc/stram-quant/m6-approval.env
AUTH_CONFIG=/etc/stram-quant/auth.env
SECRET_DIR=/etc/stram-quant/secrets
BACKUP_DIR=/var/lib/stram-quant/audit-backups
BACKUP_SERVICE=/etc/systemd/system/stram-quant-audit-backup.service
BACKUP_TIMER=/etc/systemd/system/stram-quant-audit-backup.timer
EGRESS_RULESET=/etc/stram-quant/stram-quant-egress.nft
EGRESS_UNIT=/etc/systemd/system/stram-quant-egress.service
PERSISTENCE_MARKER=/etc/stram-quant/m6-persistence-verified
QUANT_QUADLET_DIR="$QUANT_HOME/.config/containers/systemd"
QUANT_QUADLET="$QUANT_QUADLET_DIR/stram-quant-mcp.container"
AUTH_CONFIG_DIR="$AUTH_HOME/.config/stram-auth"
AUTH_QUADLET_DIR="$AUTH_HOME/.config/containers/systemd"
AUTH_SUBID_START=362144
AUTH_SUBID_COUNT=65536
EXPECTED_COMMIT=d19133350dfba394dac94f7e5615def4a366a3cc

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [ -f "$SCRIPT_DIR/keycloak/stram-staging-realm.json" ]; then
  REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
else
  REPO_ROOT=${STRAM_QUANT_REPO:-/home/siddu/Apps/stram/stram_quant_mcp}
fi
IMAGE_ARTIFACT_DIR=${STRAM_IMAGE_ARTIFACT_DIR:-$REPO_ROOT/build/m6}
REALM_TEMPLATE="$REPO_ROOT/deploy/keycloak/stram-staging-realm.json"
STAGING_VERIFIER="$REPO_ROOT/tools/staging_acceptance.py"
STAGING_CALL_GENERATOR="$REPO_ROOT/tools/generate_staging_calls.py"
NEGATIVE_TOKEN_GENERATOR="$REPO_ROOT/tools/keycloak_negative_tokens.py"

fail() { printf 'BLOCKED: %s\n' "$*" >&2; exit 1; }
pass() { printf 'PASS: %s\n' "$*"; }
not_verified() { printf 'NOT VERIFIED: %s\n' "$*"; }
msg() { printf '\n== %s ==\n' "$*"; }
require_root() { [ "$(id -u)" = 0 ] || fail 'Run this mutating command via sudo bash.'; }
require_command() { command -v "$1" >/dev/null || fail "Required command is missing: $1"; }

runtime_for() {
  local user_name=$1 user_id
  user_id=$(id -u "$user_name") || fail "Missing service account: $user_name"
  [ -d "/run/user/$user_id" ] || fail "User runtime directory is missing: /run/user/$user_id"
  printf '/run/user/%s\n' "$user_id"
}

as_user() {
  local user_name=$1 user_home=$2 runtime
  shift 2
  runtime=$(runtime_for "$user_name")
  (cd /tmp && runuser -u "$user_name" -- env \
    HOME="$user_home" XDG_RUNTIME_DIR="$runtime" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" "$@")
}
as_quant() { as_user "$QUANT_USER" "$QUANT_HOME" "$@"; }
as_auth() { as_user "$AUTH_USER" "$AUTH_HOME" "$@"; }

env_value() {
  python3 - "$1" "$2" <<'PY'
import pathlib, re, sys
path, wanted = pathlib.Path(sys.argv[1]), sys.argv[2]
found = None
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit("malformed configuration")
    key, value = line.split("=", 1)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key == wanted and found is not None:
        raise SystemExit("invalid or repeated configuration field")
    if key == wanted:
        found = value.strip()
if found is None:
    raise SystemExit("missing configuration field: " + wanted)
print(found)
PY
}

digest_ref() {
  [[ $1 =~ ^[A-Za-z0-9._:/-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$ ]]
}

auth_values() {
  [ -f "$AUTH_CONFIG" ] || fail "Authentication config is missing: $AUTH_CONFIG"
  mapfile -t AUTH_VALUES < <(python3 - "$AUTH_CONFIG" <<'PY'
import pathlib, re, sys
from urllib.parse import urlsplit

values = {}
for raw in pathlib.Path(sys.argv[1]).read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
        continue
    if "=" not in line:
        raise SystemExit("BLOCKED: malformed auth.env")
    key, value = line.split("=", 1)
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
        raise SystemExit("BLOCKED: invalid or repeated auth.env field")
    values[key] = value.strip()
required = (
    "KEYCLOAK_BASE_IMAGE_REF", "KEYCLOAK_IMAGE_REF", "POSTGRES_BASE_IMAGE_REF",
    "POSTGRES_IMAGE_REF",
    "KEYCLOAK_PUBLIC_URL", "KEYCLOAK_ADMIN_URL", "KEYCLOAK_REALM",
    "KEYCLOAK_DB_NAME", "KEYCLOAK_DB_USER", "KEYCLOAK_HOST_PORT",
    "KEYCLOAK_MANAGEMENT_PORT", "STRAM_MCP_RESOURCE_URL", "STRAM_MCP_TENANT_ID",
    "STRAM_MCP_ALLOWED_HOSTS", "STRAM_PRIVATE_HOST_IPV4", "STRAM_PRIVATE_CA_FILE",
)
for key in required:
    if key not in values:
        raise SystemExit("BLOCKED: missing auth.env field " + key)
for key in ("KEYCLOAK_BASE_IMAGE_REF", "KEYCLOAK_IMAGE_REF", "POSTGRES_BASE_IMAGE_REF", "POSTGRES_IMAGE_REF"):
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}", values[key]):
        raise SystemExit("BLOCKED: immutable digest required for " + key)
for key in ("KEYCLOAK_PUBLIC_URL", "KEYCLOAK_ADMIN_URL", "STRAM_MCP_RESOURCE_URL"):
    parsed = urlsplit(values[key])
    if parsed.scheme != "https" or not parsed.hostname or "REPLACE_" in values[key] or parsed.username or parsed.password:
        raise SystemExit("BLOCKED: exact private HTTPS URL required for " + key)
for key in ("KEYCLOAK_PUBLIC_URL", "KEYCLOAK_ADMIN_URL"):
    parsed = urlsplit(values[key])
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise SystemExit("BLOCKED: Keycloak URLs must not contain a path, query or fragment")
resource = urlsplit(values["STRAM_MCP_RESOURCE_URL"])
if resource.path != "/mcp" or resource.query or resource.fragment:
    raise SystemExit("BLOCKED: STRAM_MCP_RESOURCE_URL must end exactly in /mcp")
if values["STRAM_MCP_ALLOWED_HOSTS"] != resource.netloc or "*" in values["STRAM_MCP_ALLOWED_HOSTS"]:
    raise SystemExit("BLOCKED: MCP host allowlist must exactly match the resource URL")
import ipaddress
private_ip = ipaddress.ip_address(values["STRAM_PRIVATE_HOST_IPV4"])
if private_ip.version != 4 or private_ip not in ipaddress.ip_network("100.64.0.0/10"):
    raise SystemExit("BLOCKED: STRAM_PRIVATE_HOST_IPV4 must be a Tailscale IPv4 address")
if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", values["STRAM_MCP_TENANT_ID"]):
    raise SystemExit("BLOCKED: invalid staging tenant")
if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", values["KEYCLOAK_REALM"]):
    raise SystemExit("BLOCKED: invalid Keycloak realm")
for key in ("KEYCLOAK_HOST_PORT", "KEYCLOAK_MANAGEMENT_PORT"):
    if not values[key].isdigit() or not 1024 <= int(values[key]) <= 65535:
        raise SystemExit("BLOCKED: invalid private listener port " + key)
for key in required:
    print(values[key])
PY
  ) || fail 'Authentication configuration validation failed.'
  [ "${#AUTH_VALUES[@]}" = 16 ] || fail 'Authentication configuration validation failed.'
  KEYCLOAK_BASE_IMAGE=${AUTH_VALUES[0]}
  KEYCLOAK_IMAGE=${AUTH_VALUES[1]}
  POSTGRES_BASE_IMAGE=${AUTH_VALUES[2]}
  POSTGRES_IMAGE=${AUTH_VALUES[3]}
  KEYCLOAK_PUBLIC_URL=${AUTH_VALUES[4]}
  KEYCLOAK_ADMIN_URL=${AUTH_VALUES[5]}
  KEYCLOAK_REALM=${AUTH_VALUES[6]}
  KEYCLOAK_DB_NAME=${AUTH_VALUES[7]}
  KEYCLOAK_DB_USER=${AUTH_VALUES[8]}
  KEYCLOAK_HOST_PORT=${AUTH_VALUES[9]}
  KEYCLOAK_MANAGEMENT_PORT=${AUTH_VALUES[10]}
  MCP_RESOURCE_URL=${AUTH_VALUES[11]}
  MCP_TENANT_ID=${AUTH_VALUES[12]}
  MCP_ALLOWED_HOSTS=${AUTH_VALUES[13]}
  PRIVATE_HOST_IPV4=${AUTH_VALUES[14]}
  PRIVATE_CA_FILE=${AUTH_VALUES[15]}
  MCP_ISSUER_URL="$KEYCLOAK_PUBLIC_URL/realms/$KEYCLOAK_REALM"
  MCP_JWKS_URL="$MCP_ISSUER_URL/protocol/openid-connect/certs"
  # The Data Plane service lives on the same private host; only its port and client differ.
  DATA_SERVICE_IPV4=${STRAM_QUANT_DATA_IPV4:-$PRIVATE_HOST_IPV4}
  DATA_HOST=${KEYCLOAK_PUBLIC_URL#https://}
  DATA_HOST=${DATA_HOST%%/*}
  DATA_URL="https://$DATA_HOST:$DATA_SERVICE_PORT"
  DATA_TOKEN_URL="$MCP_ISSUER_URL/protocol/openid-connect/token"
}

check_quant_host() {
  # The MCP no longer mounts or reads the lake; the Data Plane service owns it.
  [ "$(as_quant podman info --format '{{.Host.Security.Rootless}}')" = true ] || fail 'Quant Podman must be rootless.'
  as_quant podman image exists "$REVIEW_IMAGE" || fail "Review image is missing: $REVIEW_IMAGE"
  as_quant podman volume inspect "$AUDIT_VOLUME" >/dev/null || fail "Audit volume is missing: $AUDIT_VOLUME"
  as_quant podman secret inspect "$AUDIT_SECRET" >/dev/null || fail "Audit secret is missing: $AUDIT_SECRET"
  as_quant podman secret inspect "$DATA_SECRET" >/dev/null || fail "Data Plane client secret is missing: $DATA_SECRET"
  [ "$(as_quant podman image inspect "$REVIEW_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$EXPECTED_COMMIT" ] || fail 'Unexpected review-image source revision.'
}

probe_quant() {
  local image=${1:-$REVIEW_IMAGE}
  auth_values
  msg 'Container isolation, preflight and Data Plane probes'
  as_quant podman run --rm --pull=never --network none \
    --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 \
    --read-only --cap-drop=all --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    --mount "type=volume,src=$AUDIT_VOLUME,dst=/audit" \
    --secret "$AUDIT_SECRET,type=mount,target=audit-key,uid=10001,gid=10001,mode=0400" \
    --secret "$DATA_SECRET,type=mount,target=$DATA_SECRET_TARGET,uid=10001,gid=10001,mode=0400" \
    --entrypoint /bin/sh "$image" -eu -c '
      test "$(id -u)" = 10001
      test -s /run/secrets/audit-key
      test -s /run/secrets/data-client-secret
      test ! -e /lake
      touch /audit/.stram-test && rm /audit/.stram-test
      if touch /.stram-test 2>/dev/null; then exit 71; fi'
  as_quant podman run --rm --pull=never --network none \
    --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 --read-only \
    --cap-drop=all --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    --secret "$DATA_SECRET,type=mount,target=$DATA_SECRET_TARGET,uid=10001,gid=10001,mode=0400" \
    --env-file "$SERVICE_CONFIG" "$image" preflight
  # The one probe that needs the network: it fetches a service token and reads the policies.
  as_quant podman run --rm --pull=never --network "$QUANT_NETWORK" \
    --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 --read-only \
    --cap-drop=all --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    --secret "$DATA_SECRET,type=mount,target=$DATA_SECRET_TARGET,uid=10001,gid=10001,mode=0400" \
    --add-host "$MCP_ALLOWED_HOSTS:$PRIVATE_HOST_IPV4" \
    --add-host "$DATA_HOST:$DATA_SERVICE_IPV4" \
    --env-file "$SERVICE_CONFIG" "$image" data-plane-probe
  pass 'container isolation, preflight and live Data Plane probes passed (no lake mount)'
}

write_initial_configs() {
  install -d -o "$QUANT_USER" -g "$QUANT_USER" -m 0700 "$QUANT_HOME/.config" "$CONFIG_DIR" "$QUANT_HOME/.config/containers" "$QUANT_QUADLET_DIR"
  install -d -o root -g root -m 0700 /etc/stram-quant "$SECRET_DIR"
  if [ ! -e "$SERVICE_CONFIG" ]; then
    install -o "$QUANT_USER" -g "$QUANT_USER" -m 0600 /dev/null "$SERVICE_CONFIG"
    cat > "$SERVICE_CONFIG" <<'EOF'
# Non-secret configuration. Do not add tokens, passwords or keys.
STRAM_MCP_DATA_URL=https://CHANGE_ME.invalid:8444
STRAM_MCP_DATA_TOKEN_URL=https://CHANGE_ME.invalid/realms/stram-staging/protocol/openid-connect/token
STRAM_MCP_DATA_CLIENT_ID=stram-data-mcp-staging
STRAM_MCP_DATA_CLIENT_SECRET_FILE=/run/secrets/data-client-secret
STRAM_MCP_AUDIT_DB=/audit/audit.sqlite3
STRAM_MCP_AUDIT_KEY_FILE=/run/secrets/audit-key
STRAM_MCP_ISSUER_URL=https://CHANGE_ME.invalid/realms/stram-staging
STRAM_MCP_RESOURCE_URL=https://CHANGE_ME.invalid/mcp
STRAM_MCP_JWKS_URL=https://CHANGE_ME.invalid/realms/stram-staging/protocol/openid-connect/certs
STRAM_MCP_TENANT_ID=CHANGE_ME
STRAM_MCP_ALLOWED_HOSTS=CHANGE_ME.invalid
STRAM_MCP_ALLOWED_ORIGINS=
STRAM_MCP_BIND_HOST=0.0.0.0
STRAM_MCP_BIND_PORT=8000
STRAM_MCP_DATA_MAX_CONCURRENT=4
STRAM_MCP_DATA_BREAKER_THRESHOLD=5
STRAM_MCP_DATA_BREAKER_COOLDOWN_SECONDS=15
EOF
    chown "$QUANT_USER:$QUANT_USER" "$SERVICE_CONFIG"
  fi
  if [ ! -e "$AUTH_CONFIG" ]; then
    install -o root -g root -m 0600 "$REPO_ROOT/deploy/auth.env.example" "$AUTH_CONFIG"
  fi
  if [ ! -e "$APPROVAL" ]; then
    install -o root -g root -m 0600 /dev/null "$APPROVAL"
    cat > "$APPROVAL" <<'EOF'
# Evidence gates. Set to yes only after independent review of real evidence.
APPROVED_IMAGE_REF=registry.example.invalid/stram-quant-mcp@sha256:CHANGE_ME
ROLLBACK_IMAGE_REF=registry.example.invalid/stram-quant-mcp@sha256:CHANGE_ME
M6_STAGING_AUTHORIZED=no
SBOM_PROVENANCE_VULNERABILITY_APPROVED=no
PRIVATE_TLS_AND_HOST_POLICY_VERIFIED=no
FIREWALL_EGRESS_IPV4_IPV6_VERIFIED=no
NONPROD_OIDC_AND_JWKS_VERIFIED=no
AUDIT_BACKUP_CHECKPOINT_RETENTION_VERIFIED=no
ROLLBACK_IMAGE_AND_DRILL_VERIFIED=no
ROLLBACK_AUTHORIZED=no
EOF
  fi
  [ "$(stat -c '%U:%a' "$SERVICE_CONFIG")" = "$QUANT_USER:600" ] || fail 'service.env must be stram-quant:600.'
  [ "$(stat -c '%U:%a' "$AUTH_CONFIG")" = root:600 ] || fail 'auth.env must be root:600.'
  [ "$(stat -c '%U:%a' "$APPROVAL")" = root:600 ] || fail 'm6-approval.env must be root:600.'
}

wire_service_config() {
  auth_values
  python3 - "$SERVICE_CONFIG" "$MCP_ISSUER_URL" "$MCP_RESOURCE_URL" "$MCP_JWKS_URL" "$MCP_TENANT_ID" "$MCP_ALLOWED_HOSTS" "$DATA_URL" "$DATA_TOKEN_URL" "$DATA_CLIENT_ID" <<'PY'
import os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
updates = dict(zip(
    ("STRAM_MCP_ISSUER_URL", "STRAM_MCP_RESOURCE_URL", "STRAM_MCP_JWKS_URL", "STRAM_MCP_TENANT_ID", "STRAM_MCP_ALLOWED_HOSTS", "STRAM_MCP_DATA_URL", "STRAM_MCP_DATA_TOKEN_URL", "STRAM_MCP_DATA_CLIENT_ID"),
    sys.argv[2:], strict=True,
))
bounded_defaults = {
    "STRAM_MCP_DATA_CLIENT_SECRET_FILE": "/run/secrets/data-client-secret",
    "STRAM_MCP_DATA_MAX_CONCURRENT": "4",
    "STRAM_MCP_DATA_BREAKER_THRESHOLD": "5",
    "STRAM_MCP_DATA_BREAKER_COOLDOWN_SECONDS": "15",
}
lines = path.read_text().splitlines()
seen = set()
result = []
for line in lines:
    if "=" not in line or line.lstrip().startswith("#"):
        result.append(line)
        continue
    key, old = line.split("=", 1)
    if key in updates:
        if key in seen:
            raise SystemExit("BLOCKED: repeated service.env field " + key)
        if old != updates[key] and "CHANGE_ME" not in old and ".invalid" not in old:
            raise SystemExit("BLOCKED: existing service.env conflicts with auth.env at " + key)
        result.append(key + "=" + updates[key])
        seen.add(key)
    else:
        result.append(line)
if seen != updates.keys():
    raise SystemExit("BLOCKED: service.env is missing required OAuth fields")
existing = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in result if "=" in line and not line.lstrip().startswith("#")}
for key, value in bounded_defaults.items():
    if key in existing and existing[key] != value:
        raise SystemExit("BLOCKED: existing service.env conflicts with bounded default at " + key)
    if key not in existing:
        result.append(key + "=" + value)
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix="service.env.")
try:
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(result) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
  chown "$QUANT_USER:$QUANT_USER" "$SERVICE_CONFIG"
  pass 'service.env is wired to the exact private issuer, audience, JWKS, tenant and Host'
}

install_same_or_fail() {
  local source=$1 destination=$2 owner=$3
  if [ -e "$destination" ] && ! cmp -s "$source" "$destination"; then
    fail "Refusing to overwrite drifted generated file: $destination"
  fi
  install -o "$owner" -g "$owner" -m 0600 "$source" "$destination"
}

ensure_secret_file() {
  local path=$1 bytes=$2
  if [ ! -e "$path" ]; then
    openssl rand -base64 "$bytes" | tr '+/' '-_' | tr -d '=\r\n' > "$path"
    chmod 0400 "$path"
    chown root:root "$path"
  fi
  [ "$(stat -c '%U:%a' "$path")" = root:400 ] || fail "Secret file has unsafe ownership or mode: $path"
}

ensure_urlsafe_secret_file() {
  local path=$1 bytes=$2
  ensure_secret_file "$path" "$bytes"
  if grep -q '[+/=\r\n]' "$path" || [ "$(wc -l < "$path")" -ne 0 ]; then
    local tmp="${path}.tmp"
    tr '+/' '-_' < "$path" | tr -d '=\r\n' > "$tmp"
    chmod 0400 "$tmp"
    chown root:root "$tmp"
    mv -f "$tmp" "$path"
  fi
  [ "$(stat -c '%U:%a' "$path")" = root:400 ] || fail "Secret file has unsafe ownership or mode: $path"
}

ensure_auth_podman_secret() {
  local name=$1 source=$2 replace=${3:-no}
  if ! as_auth podman secret inspect "$name" >/dev/null 2>&1; then
    tr -d '\r\n' < "$source" | as_auth podman secret create "$name" - >/dev/null
  elif [ "$replace" = yes ]; then
    tr -d '\r\n' < "$source" | as_auth podman secret create --replace "$name" - >/dev/null 2>&1 || true
  fi
}

render_auth_quadlets() {
  cat > "$AUTH_QUADLET_DIR/stram-keycloak-db.container" <<EOF
[Unit]
Description=Stram private Keycloak PostgreSQL
StartLimitIntervalSec=300
StartLimitBurst=3

[Container]
Image=$POSTGRES_IMAGE
ContainerName=stram-keycloak-db
Pull=never
User=999:999
UserNS=keep-id:uid=999,gid=999
ReadOnly=true
ReadOnlyTmpfs=false
NoNewPrivileges=true
DropCapability=all
PidsLimit=128
Memory=768m
Network=$AUTH_NETWORK
NetworkAlias=stram-keycloak-db
Volume=stram-keycloak-db:/var/lib/postgresql/data:rw,nosuid,nodev,noexec,nocopy
Tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m
Tmpfs=/run/postgresql:rw,noexec,nosuid,nodev,size=16m
Secret=stram-keycloak-db-password,type=mount,target=db-password,uid=999,gid=999,mode=0400
Environment=POSTGRES_DB=$KEYCLOAK_DB_NAME
Environment=POSTGRES_USER=$KEYCLOAK_DB_USER
Environment=POSTGRES_PASSWORD_FILE=/run/secrets/db-password
Environment="POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=scram-sha-256 --data-checksums"
HealthCmd=/usr/bin/pg_isready -U $KEYCLOAK_DB_USER -d $KEYCLOAK_DB_NAME
HealthInterval=10s
HealthTimeout=5s
HealthRetries=12
Notify=healthy
StopTimeout=30

[Service]
Restart=on-failure
RestartSec=10s
CPUQuota=100%
TimeoutStartSec=180
TimeoutStopSec=45
EOF
  cat > "$AUTH_QUADLET_DIR/stram-keycloak.container" <<EOF
[Unit]
Description=Stram private staging Keycloak
Requires=stram-keycloak-db.service
After=stram-keycloak-db.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Container]
Image=$KEYCLOAK_IMAGE
ContainerName=stram-keycloak
Pull=never
User=1000:0
UserNS=keep-id:uid=1000,gid=1000
ReadOnly=true
ReadOnlyTmpfs=false
NoNewPrivileges=true
DropCapability=all
PidsLimit=384
Memory=1536m
Network=$AUTH_NETWORK
Volume=$AUTH_CONFIG_DIR/import:/opt/keycloak/data/import:ro,nosuid,nodev,noexec
Tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m
Tmpfs=/opt/keycloak/data/tmp:rw,noexec,nosuid,nodev,size=64m
Secret=stram-keycloak-db-password,type=env,target=KCRAW_DB_PASSWORD
Secret=stram-keycloak-bootstrap-password,type=env,target=KC_BOOTSTRAP_ADMIN_PASSWORD
Secret=stram-keycloak-smoke-secret,type=env,target=STRAM_SMOKE_CLIENT_SECRET
Secret=stram-keycloak-discovery-secret,type=env,target=STRAM_DISCOVERY_CLIENT_SECRET
Environment=KC_BOOTSTRAP_ADMIN_USERNAME=stram-bootstrap-admin
Environment=KC_DB=postgres
Environment=KC_DB_URL=jdbc:postgresql://stram-keycloak-db:5432/$KEYCLOAK_DB_NAME
Environment=KC_DB_USERNAME=$KEYCLOAK_DB_USER
Environment=KC_HOSTNAME=$KEYCLOAK_PUBLIC_URL
Environment=KC_HOSTNAME_ADMIN=$KEYCLOAK_ADMIN_URL
Environment=KC_HOSTNAME_STRICT=true
Environment=KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false
Environment=KC_HTTP_ENABLED=true
Environment=KC_PROXY_HEADERS=xforwarded
Environment=KC_HTTP_MAX_QUEUED_REQUESTS=64
Environment=KC_CACHE=local
Environment=KC_HEALTH_ENABLED=true
Environment=KC_METRICS_ENABLED=true
Environment=STRAM_KEYCLOAK_REALM=$KEYCLOAK_REALM
Environment=STRAM_MCP_RESOURCE_URL=$MCP_RESOURCE_URL
Environment=STRAM_MCP_TENANT_ID=$MCP_TENANT_ID
PublishPort=127.0.0.1:$KEYCLOAK_HOST_PORT:8080
PublishPort=127.0.0.1:$KEYCLOAK_MANAGEMENT_PORT:9000
StopTimeout=30
Exec=start --optimized --import-realm

[Service]
Restart=on-failure
RestartSec=10s
CPUQuota=200%
TimeoutStartSec=180
TimeoutStopSec=45
EOF
  if [ -e "$PERSISTENCE_MARKER" ]; then
    for quadlet in "$AUTH_QUADLET_DIR/stram-keycloak-db.container" "$AUTH_QUADLET_DIR/stram-keycloak.container"; do
      cat >> "$quadlet" <<'EOF'

[Install]
WantedBy=default.target
EOF
    done
  fi
  chown "$AUTH_USER:$AUTH_USER" "$AUTH_QUADLET_DIR"/*.container
  chmod 0600 "$AUTH_QUADLET_DIR"/*.container
}

doctor() {
  msg 'Read-only SIDDU deployment inventory'
  printf 'UTC: %s\n' "$(date -u +%FT%TZ)"
  if [ "$(id -u)" != 0 ]; then
    not_verified 'run doctor through sudo for service-identity Podman and protected config checks'
  else
    if id "$QUANT_USER" >/dev/null 2>&1; then pass 'stram-quant service identity exists'; else printf 'FAIL: stram-quant identity is missing\n'; fi
    if id "$AUTH_USER" >/dev/null 2>&1; then pass 'stram-auth service identity exists'; else not_verified 'stram-auth identity requires operator-approved creation'; fi
    if findmnt -M "$LAKE" >/dev/null 2>&1; then pass 'Stock lake mount exists (read by the Data Plane service, not by this MCP)'; else not_verified 'Stock lake mount is not visible on this host'; fi
    if [ -f "$SERVICE_CONFIG" ]; then pass 'Quant service configuration exists'; else not_verified 'Quant service configuration has not been prepared'; fi
    if [ -f "$AUTH_CONFIG" ]; then pass 'authentication configuration exists'; else not_verified 'authentication configuration has not been prepared'; fi
  fi
  if tailscale status --json >/dev/null 2>&1; then pass 'Tailscale state is readable'; else not_verified 'Tailscale state is unavailable'; fi
  if tailscale serve status 2>/dev/null | grep -q .; then pass 'a Tailscale Serve route exists and still requires topology review'; else not_verified 'no readable Tailscale Serve route'; fi
  if [ -f "$AUTH_CONFIG" ]; then discovery_routes discovery-routes status; fi
  if command -v caddy >/dev/null || command -v nginx >/dev/null || command -v traefik >/dev/null; then
    pass 'a reverse-proxy binary is installed; configuration still requires review'
  else
    not_verified 'no Caddy, Nginx or Traefik binary found'
  fi
  if ss -H -lnt '( sport = :8800 or sport = :9080 or sport = :9001 )' 2>/dev/null | grep -q .; then
    not_verified 'one or more staging ports are already listening; inspect ownership before activation'
  else
    pass 'staging loopback ports 8800, 9080 and 9001 are free'
  fi
  command -v nft >/dev/null && command -v firewall-cmd >/dev/null && pass 'host firewall tooling is installed'
  not_verified 'private DNS, trusted HTTPS certificates, proxy path restrictions and egress rules require operator evidence'
}

prepare() {
  require_root
  check_quant_host
  probe_quant
  write_initial_configs
  msg 'Preparation complete; no listener or service was started'
  printf 'Review and edit: %s, %s, %s\n' "$AUTH_CONFIG" "$SERVICE_CONFIG" "$APPROVAL"
}

provision_auth() {
  require_root
  if ! id "$AUTH_USER" >/dev/null 2>&1; then
    useradd --create-home --home-dir "$AUTH_HOME" --shell /usr/bin/nologin "$AUTH_USER"
  fi
  [ "$(getent passwd "$AUTH_USER" | cut -d: -f6)" = "$AUTH_HOME" ] || fail 'stram-auth has an unexpected home directory.'
  [ "$(getent passwd "$AUTH_USER" | cut -d: -f7)" = /usr/bin/nologin ] || fail 'stram-auth must use /usr/bin/nologin.'
  python3 - /etc/subuid /etc/subgid "$AUTH_USER" "$AUTH_SUBID_START" "$AUTH_SUBID_COUNT" <<'PY'
import pathlib, sys
user = sys.argv[3]
start, count = map(int, sys.argv[4:])
end = start + count
for name in sys.argv[1:3]:
    for raw in pathlib.Path(name).read_text().splitlines():
        parts = raw.split(":")
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        if parts[0] == user:
            continue
        other_start, other_count = map(int, parts[1:])
        if max(start, other_start) < min(end, other_start + other_count):
            raise SystemExit(f"BLOCKED: requested subordinate ID range overlaps {name}: {raw}")
PY
  if ! grep -qx "$AUTH_USER:$AUTH_SUBID_START:$AUTH_SUBID_COUNT" /etc/subuid || ! grep -qx "$AUTH_USER:$AUTH_SUBID_START:$AUTH_SUBID_COUNT" /etc/subgid; then
    grep -q "^$AUTH_USER:" /etc/subuid || usermod --add-subuids "$AUTH_SUBID_START-$((AUTH_SUBID_START + AUTH_SUBID_COUNT - 1))" "$AUTH_USER"
    grep -q "^$AUTH_USER:" /etc/subgid || usermod --add-subgids "$AUTH_SUBID_START-$((AUTH_SUBID_START + AUTH_SUBID_COUNT - 1))" "$AUTH_USER"
  fi
  grep -qx "$AUTH_USER:$AUTH_SUBID_START:$AUTH_SUBID_COUNT" /etc/subuid || fail 'Unexpected stram-auth subordinate UID allocation.'
  grep -qx "$AUTH_USER:$AUTH_SUBID_START:$AUTH_SUBID_COUNT" /etc/subgid || fail 'Unexpected stram-auth subordinate GID allocation.'
  loginctl enable-linger "$AUTH_USER"
  systemctl start "user@$(id -u "$AUTH_USER").service"
  install -d -o "$AUTH_USER" -g "$AUTH_USER" -m 0700 "$AUTH_HOME/.config" "$AUTH_HOME/.config/containers" "$AUTH_QUADLET_DIR"
  chown root:"$QUANT_USER" /srv/stram-quant
  chmod 0750 /srv/stram-quant
  as_auth test ! -r "$LAKE/_manifests/economic_observation/_active.json" || fail 'stram-auth can read the Stock lake.'
  if ! as_quant podman network exists "$QUANT_NETWORK"; then
    as_quant podman network create "$QUANT_NETWORK" >/dev/null
  fi
  [ "$(as_auth podman info --format '{{.Host.Security.Rootless}}')" = true ] || fail 'Auth Podman must be rootless.'
  pass 'dedicated stram-auth rootless identity, subordinate IDs, linger and lake isolation are provisioned'
}

import_images() {
  require_root
  auth_values
  local approved_image rollback_image
  approved_image=$(env_value "$APPROVAL" APPROVED_IMAGE_REF)
  rollback_image=$(env_value "$APPROVAL" ROLLBACK_IMAGE_REF)
  digest_ref "$approved_image" || fail 'APPROVED_IMAGE_REF is not an immutable digest reference.'
  digest_ref "$rollback_image" || fail 'ROLLBACK_IMAGE_REF is not an immutable digest reference.'
  local keycloak_archive="$IMAGE_ARTIFACT_DIR/keycloak-26.8.1.tar"
  local postgres_archive="$IMAGE_ARTIFACT_DIR/postgres-17.11-stram1.tar"
  local quant_archive="$IMAGE_ARTIFACT_DIR/quant-candidate.tar"
  local rollback_archive="$IMAGE_ARTIFACT_DIR/quant-review.tar"
  for archive in "$keycloak_archive" "$postgres_archive" "$quant_archive" "$rollback_archive"; do
    [ -s "$archive" ] || fail "Missing image archive: $archive"
  done
  as_auth podman image exists "$KEYCLOAK_IMAGE" || as_auth podman load < "$keycloak_archive" >/dev/null
  as_auth podman image exists "$POSTGRES_IMAGE" || as_auth podman load < "$postgres_archive" >/dev/null
  as_quant podman image exists "$approved_image" || as_quant podman load < "$quant_archive" >/dev/null
  if ! as_quant podman image exists "$rollback_image" || ! as_quant podman image exists "$REVIEW_IMAGE"; then
    as_quant podman load < "$rollback_archive" >/dev/null
  fi
  as_auth podman image exists "$KEYCLOAK_IMAGE" || fail 'Imported Keycloak digest does not match auth.env.'
  as_auth podman image exists "$POSTGRES_IMAGE" || fail 'Imported PostgreSQL digest does not match auth.env.'
  as_quant podman image exists "$approved_image" || fail 'Imported Quant digest does not match m6-approval.env.'
  as_quant podman image exists "$rollback_image" || fail 'Imported rollback digest does not match m6-approval.env.'
  pass 'reviewed immutable images are imported into the separate rootless service stores'
}

auth_prepare() {
  require_root
  require_command openssl
  id "$AUTH_USER" >/dev/null 2>&1 || fail 'Create the dedicated non-login stram-auth identity only after operator approval.'
  write_initial_configs
  auth_values
  wire_service_config
  as_auth podman image exists "$KEYCLOAK_IMAGE" || fail 'Approved optimized Keycloak digest is not in stram-auth Podman storage.'
  as_auth podman image exists "$POSTGRES_IMAGE" || fail 'Approved PostgreSQL digest is not in stram-auth Podman storage.'
  [ "$(as_auth podman image inspect "$KEYCLOAK_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.base.name"}}')" = "$KEYCLOAK_BASE_IMAGE" ] || fail 'Optimized Keycloak image base digest label does not match auth.env.'
  [ "$(as_auth podman image inspect "$POSTGRES_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.base.name"}}')" = "$POSTGRES_BASE_IMAGE" ] || fail 'Hardened PostgreSQL image base digest label does not match auth.env.'
  install -d -o "$AUTH_USER" -g "$AUTH_USER" -m 0700 "$AUTH_HOME/.config" "$AUTH_CONFIG_DIR" "$AUTH_CONFIG_DIR/import" "$AUTH_HOME/.config/containers" "$AUTH_QUADLET_DIR"
  install_same_or_fail "$REALM_TEMPLATE" "$AUTH_CONFIG_DIR/import/$KEYCLOAK_REALM-realm.json" "$AUTH_USER"
  ensure_secret_file "$SECRET_DIR/keycloak-db-password" 48
  ensure_secret_file "$SECRET_DIR/keycloak-bootstrap-password" 48
  ensure_urlsafe_secret_file "$SECRET_DIR/keycloak-smoke-client-secret" 48
  ensure_urlsafe_secret_file "$SECRET_DIR/keycloak-discovery-client-secret" 48
  ensure_auth_podman_secret stram-keycloak-db-password "$SECRET_DIR/keycloak-db-password"
  ensure_auth_podman_secret stram-keycloak-bootstrap-password "$SECRET_DIR/keycloak-bootstrap-password"
  ensure_auth_podman_secret stram-keycloak-smoke-secret "$SECRET_DIR/keycloak-smoke-client-secret" yes
  ensure_auth_podman_secret stram-keycloak-discovery-secret "$SECRET_DIR/keycloak-discovery-client-secret" yes
  as_auth podman network exists "$AUTH_NETWORK" || as_auth podman network create --internal "$AUTH_NETWORK" >/dev/null
  as_auth podman volume exists stram-keycloak-db || as_auth podman volume create stram-keycloak-db >/dev/null
  render_auth_quadlets
  as_auth systemctl --user daemon-reload
  as_auth systemctl --user cat stram-keycloak-db.service >/dev/null || fail 'PostgreSQL Quadlet generation failed.'
  as_auth systemctl --user cat stram-keycloak.service >/dev/null || fail 'Keycloak Quadlet generation failed.'
  pass 'Keycloak/PostgreSQL secrets, storage, internal network and Quadlets are staged'
  not_verified 'units remain stopped and disabled until explicit operator authorization'
}

install_egress() {
  require_root
  require_command nft
  require_command tailscale
  auth_values
  local current_ip quant_uid
  current_ip=$(tailscale ip -4) || fail 'Unable to read the current Tailscale IPv4 address.'
  [ "$current_ip" = "$PRIVATE_HOST_IPV4" ] || fail 'auth.env Tailscale IPv4 does not match this node.'
  quant_uid=$(id -u "$QUANT_USER") || fail 'Missing stram-quant identity.'
  local quant_was_active=no
  if as_quant systemctl --user is-active --quiet stram-quant-mcp.service; then
    quant_was_active=yes
    as_quant systemctl --user stop stram-quant-mcp.service
  fi
  cat > "$EGRESS_RULESET" <<EOF
table inet stram_quant {
  chain output {
    type filter hook output priority filter; policy accept;
    meta skuid $quant_uid oifname "lo" ip saddr 127.0.0.1 ip daddr 127.0.0.1 tcp sport 8800 tcp flags != syn / syn,ack counter accept
    meta skuid $quant_uid ip daddr $PRIVATE_HOST_IPV4 tcp dport 443 counter accept
    meta skuid $quant_uid ip daddr $DATA_SERVICE_IPV4 tcp dport $DATA_SERVICE_PORT counter accept
    meta skuid $quant_uid counter reject with icmpx type admin-prohibited
  }
}
EOF
  chmod 0600 "$EGRESS_RULESET"
  chown root:root "$EGRESS_RULESET"
  cat > "$EGRESS_UNIT" <<EOF
[Unit]
Description=Stram Quant MCP UID-scoped OIDC-only egress policy
After=firewalld.service tailscaled.service
Wants=tailscaled.service
Before=user@$quant_uid.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/nft -f $EGRESS_RULESET
ExecStop=/usr/bin/nft delete table inet stram_quant

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "$EGRESS_UNIT"
  chown root:root "$EGRESS_UNIT"
  local user_dropin_dir="/etc/systemd/system/user@$quant_uid.service.d"
  install -d -o root -g root -m 0755 "$user_dropin_dir"
  cat > "$user_dropin_dir/stram-quant-egress.conf" <<EOF
[Unit]
Requires=stram-quant-egress.service
After=stram-quant-egress.service
EOF
  chmod 0644 "$user_dropin_dir/stram-quant-egress.conf"
  chown root:root "$user_dropin_dir/stram-quant-egress.conf"
  systemctl daemon-reload
  if systemctl is-active --quiet stram-quant-egress.service; then
    systemctl stop stram-quant-egress.service
  fi
  nft list table inet stram_quant >/dev/null 2>&1 && nft delete table inet stram_quant
  systemctl enable --now stram-quant-egress.service
  nft list chain inet stram_quant output | grep -F "meta skuid $quant_uid ip daddr $PRIVATE_HOST_IPV4 tcp dport 443" >/dev/null || fail 'Quant allowlist rule is missing.'
  nft list chain inet stram_quant output | grep -F "meta skuid $quant_uid ip daddr $DATA_SERVICE_IPV4 tcp dport $DATA_SERVICE_PORT" >/dev/null || fail 'Quant Data Plane allowlist rule is missing.'
  nft list chain inet stram_quant output | grep -F "meta skuid $quant_uid counter" >/dev/null || fail 'Quant default egress rejection rule is missing.'
  if [ "$quant_was_active" = yes ]; then as_quant systemctl --user start stram-quant-mcp.service; fi
  pass 'persistent UID-scoped egress permits only SIDDU Tailscale HTTPS plus the Data Plane service port and rejects DNS/IPv4/IPv6 alternatives'
}

auth_start() {
  require_root
  auth_values
  [ "$(env_value "$APPROVAL" M6_STAGING_AUTHORIZED)" = yes ] || fail 'M6_STAGING_AUTHORIZED is not yes.'
  [ "$(env_value "$APPROVAL" PRIVATE_TLS_AND_HOST_POLICY_VERIFIED)" = yes ] || fail 'Private TLS/Host policy evidence is not approved.'
  as_auth systemctl --user start stram-keycloak-db.service
  as_auth systemctl --user start stram-keycloak.service
  for _ in $(seq 1 120); do
    if curl --fail --silent --max-time 2 "http://127.0.0.1:$KEYCLOAK_MANAGEMENT_PORT/health/ready" >/dev/null; then
      pass 'Keycloak local management readiness passed'
      return
    fi
    sleep 1
  done
  fail 'Keycloak readiness did not pass; units remain disabled.'
}

auth_verify() {
  require_root
  auth_values
  local discovery="$MCP_ISSUER_URL/.well-known/openid-configuration" curl_args=(--fail --silent --show-error --max-time 10)
  if [ -n "$PRIVATE_CA_FILE" ]; then curl_args+=(--cacert "$PRIVATE_CA_FILE"); fi
  local discovery_json
  discovery_json=$(curl "${curl_args[@]}" "$discovery") || fail 'Trusted Keycloak discovery request failed.'
  python3 - "$MCP_ISSUER_URL" "$MCP_JWKS_URL" "$discovery_json" <<'PY'
import json, sys
issuer, jwks, raw = sys.argv[1:]
value = json.loads(raw)
if value.get("issuer") != issuer or value.get("jwks_uri") != jwks:
    raise SystemExit("BLOCKED: discovery issuer/JWKS drift")
print("PASS: host-trusted Keycloak discovery exactly matches issuer and JWKS")
PY
  local image
  image=$(env_value "$APPROVAL" APPROVED_IMAGE_REF)
  digest_ref "$image" || image=$REVIEW_IMAGE
  as_quant podman run --rm --pull=never --network "$QUANT_NETWORK" \
    --add-host "$MCP_ALLOWED_HOSTS:$PRIVATE_HOST_IPV4" \
    --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 --read-only \
    --cap-drop=all --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    --entrypoint /opt/venv/bin/python "$image" \
    - "$discovery" "$MCP_ISSUER_URL" "$MCP_JWKS_URL" <<'PY'
import json, ssl, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=10, context=ssl.create_default_context()) as response:
    value = json.load(response)
if value.get("issuer") != sys.argv[2] or value.get("jwks_uri") != sys.argv[3]:
    raise SystemExit("container discovery drift")
print("PASS: MCP container validates the issuer certificate and exact discovery metadata")
PY
}

prepare_acceptance() {
  require_root
  auth_values
  [ -x "$REPO_ROOT/.venv/bin/python" ] || fail 'Frozen repository environment is missing.'
  auth_verify
  "$REPO_ROOT/.venv/bin/python" "$STAGING_CALL_GENERATOR" \
    --parquet-root "$LAKE" --output /etc/stram-quant/staging-calls.json
  local negative_args=(
    --admin-loopback "http://127.0.0.1:$KEYCLOAK_HOST_PORT" \
    --public-url "$KEYCLOAK_PUBLIC_URL" \
    --realm "$KEYCLOAK_REALM" \
    --resource "$MCP_RESOURCE_URL" \
    --tenant "$MCP_TENANT_ID" \
    --bootstrap-password-file "$SECRET_DIR/keycloak-bootstrap-password" \
    --smoke-secret-file "$SECRET_DIR/keycloak-smoke-client-secret" \
    --discovery-secret-file "$SECRET_DIR/keycloak-discovery-client-secret" \
    --output-dir /etc/stram-quant/negative-tokens
  )
  if [ -n "$PRIVATE_CA_FILE" ]; then negative_args+=(--ca-file "$PRIVATE_CA_FILE"); fi
  "$REPO_ROOT/.venv/bin/python" "$NEGATIVE_TOKEN_GENERATOR" "${negative_args[@]}"
  chown -R root:root /etc/stram-quant/negative-tokens
  chmod 0700 /etc/stram-quant/negative-tokens
  pass 'real-lake calls and genuine Keycloak-signed negative JWT evidence are prepared'
}

validate_approval() {
  auth_values
  APPROVED_IMAGE=$(python3 - "$SERVICE_CONFIG" "$APPROVAL" "$MCP_ISSUER_URL" "$MCP_RESOURCE_URL" "$MCP_JWKS_URL" "$DATA_URL" "$DATA_TOKEN_URL" "$DATA_CLIENT_ID" <<'PY'
import pathlib, re, sys
def parse(path):
    values = {}
    for raw in pathlib.Path(path).read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"): continue
        if "=" not in line: raise ValueError("malformed entry")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values: raise ValueError("invalid or repeated field")
        values[key] = value.strip()
    return values
try:
    service, approval = parse(sys.argv[1]), parse(sys.argv[2])
    exact = {"STRAM_MCP_ISSUER_URL": sys.argv[3], "STRAM_MCP_RESOURCE_URL": sys.argv[4], "STRAM_MCP_JWKS_URL": sys.argv[5]}
    exact.update({"STRAM_MCP_DATA_URL": sys.argv[6], "STRAM_MCP_DATA_TOKEN_URL": sys.argv[7], "STRAM_MCP_DATA_CLIENT_ID": sys.argv[8], "STRAM_MCP_DATA_CLIENT_SECRET_FILE": "/run/secrets/data-client-secret", "STRAM_MCP_AUDIT_DB": "/audit/audit.sqlite3", "STRAM_MCP_AUDIT_KEY_FILE": "/run/secrets/audit-key", "STRAM_MCP_BIND_HOST": "0.0.0.0", "STRAM_MCP_BIND_PORT": "8000", "STRAM_MCP_ALLOWED_ORIGINS": "", "STRAM_MCP_DATA_MAX_CONCURRENT": "4", "STRAM_MCP_DATA_BREAKER_THRESHOLD": "5", "STRAM_MCP_DATA_BREAKER_COOLDOWN_SECONDS": "15"})
    for key, value in exact.items():
        if service.get(key) != value: raise ValueError("incorrect " + key)
    if any(("SECRET" in key and not key.endswith("_SECRET_FILE")) or ("TOKEN" in key and not key.endswith("_TOKEN_URL")) or "PASSWORD" in key or key in {"STRAM_MCP_ALLOW_INSECURE_HTTP", "STRAM_MCP_AUDIT_KEY"} for key in service):
        raise ValueError("secrets and insecure HTTP are prohibited in service.env")
    image = approval.get("APPROVED_IMAGE_REF", "")
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+(?::[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}", image):
        raise ValueError("approved registry digest is missing")
    # Backup and rollback evidence is necessarily produced after first activation.
    # Those post-activation controls remain explicit gates in m6-approval.env and
    # are checked by the operational handoff, not forged to break this dependency.
    gates = ("M6_STAGING_AUTHORIZED", "SBOM_PROVENANCE_VULNERABILITY_APPROVED", "PRIVATE_TLS_AND_HOST_POLICY_VERIFIED", "FIREWALL_EGRESS_IPV4_IPV6_VERIFIED", "NONPROD_OIDC_AND_JWKS_VERIFIED")
    for gate in gates:
        if approval.get(gate) != "yes": raise ValueError("staging not approved: " + gate)
    print(image)
except ValueError as error:
    raise SystemExit("BLOCKED: " + str(error)) from None
PY
  ) || fail 'Complete and independently approve staging evidence before activation.'
  [ "$(stat -c '%U:%a' "$APPROVAL")" = root:600 ] || fail 'Approval file must be root:600.'
  [ "$(stat -c '%U:%a' "$SERVICE_CONFIG")" = "$QUANT_USER:600" ] || fail 'Service config must be stram-quant:600.'
}

render_quant_quadlet() {
  local image=$1 output=$2
  cat > "$output" <<EOF
[Unit]
Description=Stram Quant MCP isolated M6 staging
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=3

[Container]
Image=$image
ContainerName=stram-quant-mcp
Pull=never
User=10001:10001
UserNS=keep-id:uid=10001,gid=10001
ReadOnly=true
ReadOnlyTmpfs=false
NoNewPrivileges=true
DropCapability=all
PidsLimit=128
Memory=1536m
Ulimit=stack=2097152:2097152
PodmanArgs=--cpus=2
Tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m
Volume=$AUDIT_VOLUME:/audit:rw,nosuid,nodev,noexec,nocopy
Secret=$AUDIT_SECRET,type=mount,target=audit-key,uid=10001,gid=10001,mode=0400
Secret=$DATA_SECRET,type=mount,target=$DATA_SECRET_TARGET,uid=10001,gid=10001,mode=0400
EnvironmentFile=$SERVICE_CONFIG
Environment=STRAM_MCP_IMAGE_DIGEST=${image##*@}
Environment=MALLOC_ARENA_MAX=2
Environment=GLIBC_TUNABLES=glibc.malloc.arena_max=2
Network=$QUANT_NETWORK
AddHost=$MCP_ALLOWED_HOSTS:$PRIVATE_HOST_IPV4
AddHost=$DATA_HOST:$DATA_SERVICE_IPV4
PublishPort=127.0.0.1:8800:8000
StopTimeout=15

[Service]
Restart=on-failure
RestartSec=5s
CPUQuota=200%
TimeoutStartSec=90
TimeoutStopSec=25
EOF
  if [ -e "$PERSISTENCE_MARKER" ]; then
    cat >> "$output" <<'EOF'

[Install]
WantedBy=default.target
EOF
  fi
}

wait_for_mcp_readiness() {
  local timeout=${1:-30} elapsed=0 response code err
  while [ "$elapsed" -lt "$timeout" ]; do
    response=$(curl --write-out '\n%{http_code}' --silent --show-error --max-time 2 http://127.0.0.1:8800/health/ready 2>&1) && err=0 || err=$?
    code=$(tail -n 1 <<<"$response")
    if [ "$err" -eq 0 ] && [ "$code" = "200" ]; then
      return 0
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  printf 'FAIL: MCP readiness check timed out after %ds (last exit: %d, HTTP: %s, output: %s)\n' \
    "$timeout" "$err" "$code" "$(head -n -1 <<<"$response" | tr '\n' ' ')" >&2
  return 1
}

activate() {
  require_root
  check_quant_host
  validate_approval
  as_quant podman image exists "$APPROVED_IMAGE" || fail 'Approved digest is not in stram-quant Podman storage.'
  as_quant podman network exists "$QUANT_NETWORK" || fail 'Private Quant network is not configured.'
  if as_quant systemctl --user is-active --quiet stram-quant-mcp.service; then
    as_quant systemctl --user stop stram-quant-mcp.service
  fi
  ss -H -lnt '( sport = :8800 )' | grep -q . && fail 'Port 8800 is already in use.'
  probe_quant "$APPROVED_IMAGE"
  local temporary
  temporary=$(mktemp)
  render_quant_quadlet "$APPROVED_IMAGE" "$temporary"
  if [ -e "$QUANT_QUADLET" ]; then cp --preserve=mode,ownership,timestamps "$QUANT_QUADLET" "$QUANT_QUADLET.previous"; fi
  install -o "$QUANT_USER" -g "$QUANT_USER" -m 0600 "$temporary" "$QUANT_QUADLET"
  rm -f "$temporary"
  as_quant systemctl --user daemon-reload
  as_quant systemctl --user cat stram-quant-mcp.service >/dev/null || fail 'Quant Quadlet generation failed.'
  as_quant systemctl --user start stram-quant-mcp.service
  wait_for_mcp_readiness 30 || fail 'MCP readiness failed; preserve the journal and inspect the user journal.'
  pass 'staging MCP readiness passed; unit remains boot-disabled'
}

verify_data_plane_link() {
  # Fetches a service token and reads the policies from inside the running container.
  as_quant podman exec stram-quant-mcp python -m stram_quant_mcp.entrypoint data-plane-probe \
    | grep -F '"status":"data_plane_ok"' >/dev/null || fail 'The running MCP cannot reach the Data Plane service.'
  printf 'PASS: running MCP fetched a service token and reached the Data Plane service\n'
}

verify_egress_denials() {
  as_quant podman exec stram-quant-mcp python - <<'PY'
import socket
targets = (("example.com", 443, "DNS"), ("1.1.1.1", 443, "IPv4"), ("2606:4700:4700::1111", 443, "IPv6"))
for host, port, label in targets:
    try:
        connection = socket.create_connection((host, port), timeout=2)
    except OSError:
        print(f"PASS: host firewall denied non-allowlisted {label} egress")
    else:
        connection.close()
        raise SystemExit(f"FAIL: non-allowlisted {label} egress succeeded")
PY
}

run_acceptance() {
  local acceptance_args=(
    --endpoint "$MCP_RESOURCE_URL" --issuer "$MCP_ISSUER_URL" --resource "$MCP_RESOURCE_URL"
    --tenant "$MCP_TENANT_ID"
    --client-secret-file "$SECRET_DIR/keycloak-smoke-client-secret"
    --discovery-client-secret-file "$SECRET_DIR/keycloak-discovery-client-secret"
    --calls-file /etc/stram-quant/staging-calls.json
    --negative-token-dir /etc/stram-quant/negative-tokens
    --require-complete
  )
  if [ -n "$PRIVATE_CA_FILE" ]; then acceptance_args+=(--ca-file "$PRIVATE_CA_FILE"); fi
  "$REPO_ROOT/.venv/bin/python" "$STAGING_VERIFIER" "${acceptance_args[@]}"
}

verify() {
  require_root
  auth_values
  auth_verify
  curl --fail --silent --max-time 3 http://127.0.0.1:8800/health/ready >/dev/null || fail 'MCP is not ready.'
  verify_data_plane_link
  verify_egress_denials
  [ -x "$REPO_ROOT/.venv/bin/python" ] || fail 'Frozen repository environment is missing.'
  run_acceptance
}

reliability_drill() {
  require_root
  auth_values
  [ -x "$REPO_ROOT/.venv/bin/python" ] || fail 'Frozen repository environment is missing.'
  local quant_uid drill_table=stram_quant_drill
  quant_uid=$(id -u "$QUANT_USER") || fail 'Missing stram-quant identity.'
  # Simulate a Data Plane outage for the MCP user only, then always remove the simulation.
  trap 'nft delete table inet stram_quant_drill >/dev/null 2>&1 || true' EXIT
  nft add table inet "$drill_table"
  nft add chain inet "$drill_table" output '{ type filter hook output priority -10; policy accept; }'
  nft add rule inet "$drill_table" output meta skuid "$quant_uid" ip daddr "$DATA_SERVICE_IPV4" tcp dport "$DATA_SERVICE_PORT" reject
  if run_acceptance; then
    fail 'A blocked Data Plane did not produce classified, retryable tool errors.'
  fi
  curl --fail --silent --max-time 3 http://127.0.0.1:8800/health/ready >/dev/null || fail 'An upstream outage must not make the MCP unready.'
  nft delete table inet "$drill_table"
  trap - EXIT
  # The circuit breaker re-probes after its cooldown (15 seconds by default).
  sleep 20
  run_acceptance
  as_quant systemctl --user restart stram-quant-mcp.service
  wait_for_mcp_readiness 30 || fail 'Service did not recover from a controlled restart.'
  run_acceptance
  local restart_policy
  restart_policy=$(as_quant systemctl --user show stram-quant-mcp.service \
    --property=Restart --property=StartLimitBurst --property=StartLimitIntervalUSec)
  grep -q '^Restart=on-failure$' <<<"$restart_policy" || fail 'Service restart policy is not on-failure.'
  grep -q '^StartLimitBurst=3$' <<<"$restart_policy" || fail 'Service restart burst is not bounded to three.'
  grep -Eq '^StartLimitIntervalUSec=(2min|120s)$' <<<"$restart_policy" || fail 'Service restart interval is not bounded to 120 seconds.'
  pass 'Data Plane outage, circuit-breaker recovery and controlled service restart passed'
}

backup_audit() {
  require_root
  APPROVED_IMAGE=$(env_value "$APPROVAL" APPROVED_IMAGE_REF)
  digest_ref "$APPROVED_IMAGE" || fail 'Approved audit-backup image is not an immutable digest reference.'
  as_quant podman image exists "$APPROVED_IMAGE" || fail 'Approved audit-backup image is not retained locally.'
  # The parent must be traversable by the rootless service user despite umask 077.
  install -d -o root -g root -m 0755 "$(dirname "$BACKUP_DIR")"
  install -d -o "$QUANT_USER" -g "$QUANT_USER" -m 0700 "$BACKUP_DIR"
  ensure_secret_file "$SECRET_DIR/checkpoint-signing-key" 32
  local stamp image backup checkpoint verification
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  if [ -e "$BACKUP_DIR/audit-$stamp.sqlite3" ]; then
    sleep 1
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
  fi
  image=$APPROVED_IMAGE
  backup="$BACKUP_DIR/audit-$stamp.sqlite3"
  checkpoint="$BACKUP_DIR/audit-$stamp.checkpoint.json"
  verification=$(as_quant podman run --rm --pull=never --network none \
    --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 --read-only \
    --cap-drop=all --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    --mount "type=volume,src=$AUDIT_VOLUME,dst=/audit" \
    --mount "type=bind,src=$BACKUP_DIR,dst=/backup,rw" \
    --secret "$AUDIT_SECRET,type=mount,target=audit-key,uid=10001,gid=10001,mode=0400" \
    --entrypoint /opt/venv/bin/python "$image" -m stram_quant_mcp.audit backup --db /audit/audit.sqlite3 \
    --key-file /run/secrets/audit-key --output "/backup/$(basename "$backup")")
  python3 - "$verification" "$backup" "$checkpoint" "$image" "$SECRET_DIR/checkpoint-signing-key" <<'PY'
import hashlib, hmac, json, os, pathlib, sys
verification = json.loads(sys.argv[1])
backup = pathlib.Path(sys.argv[2])
checkpoint = pathlib.Path(sys.argv[3])
image = sys.argv[4]
key_path = pathlib.Path(sys.argv[5])
payload = {
    "audit": verification,
    "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    "image_ref": image,
    "created_utc": checkpoint.name.split("audit-", 1)[1].split(".checkpoint", 1)[0],
}
encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
signature = hmac.new(key_path.read_bytes().strip(), encoded, hashlib.sha256).hexdigest()
checkpoint.write_text(json.dumps({"payload": payload, "signature": signature}, sort_keys=True, indent=2) + "\n")
os.chmod(checkpoint, 0o400)
os.chmod(backup, 0o400)
PY
  chown root:root "$backup" "$checkpoint"
  find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'audit-*.sqlite3' -o -name 'audit-*.checkpoint.json' \) \
    -mtime +30 -delete
  verify_backup "$backup" "$checkpoint"
  pass 'WAL-consistent audit backup independently verified and externally checkpointed'
}

verify_backup() {
  require_root
  local backup=${1:-} checkpoint=${2:-}
  if [ -z "$checkpoint" ]; then
    local checkpoints=("$BACKUP_DIR"/audit-*.checkpoint.json)
    [ -e "${checkpoints[0]}" ] || fail 'No audit checkpoint is available.'
    checkpoint=${checkpoints[${#checkpoints[@]}-1]}
    backup=${checkpoint%.checkpoint.json}.sqlite3
  fi
  [ -f "$backup" ] && [ -f "$checkpoint" ] || fail 'Audit backup/checkpoint pair is incomplete.'
  python3 - "$backup" "$checkpoint" "$SECRET_DIR/checkpoint-signing-key" <<'PY'
import hashlib, hmac, json, pathlib, sys
backup, checkpoint, key_path = map(pathlib.Path, sys.argv[1:])
value = json.loads(checkpoint.read_text())
payload, signature = value.get("payload"), value.get("signature")
if not isinstance(payload, dict) or not isinstance(signature, str):
    raise SystemExit("BLOCKED: invalid checkpoint structure")
encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
expected = hmac.new(key_path.read_bytes().strip(), encoded, hashlib.sha256).hexdigest()
if not hmac.compare_digest(signature, expected):
    raise SystemExit("BLOCKED: checkpoint signature is invalid")
if payload.get("backup_sha256") != hashlib.sha256(backup.read_bytes()).hexdigest():
    raise SystemExit("BLOCKED: backup digest does not match checkpoint")
PY
  local image temporary_dir status=0
  image=$(env_value "$APPROVAL" APPROVED_IMAGE_REF)
  as_quant podman image exists "$image" || fail 'Audit verification image is not retained locally.'
  temporary_dir=$(mktemp -d)
  chown "$QUANT_USER:$QUANT_USER" "$temporary_dir"
  chmod 0700 "$temporary_dir"
  # Verify a disposable copy: SQLite WAL readers need a writable directory for -shm/-wal and
  # the journal opener normalizes file mode. The checkpointed original is never mounted.
  install -o "$QUANT_USER" -g "$QUANT_USER" -m 0600 "$backup" "$temporary_dir/audit.sqlite3"
  as_quant podman run --rm --pull=never --network none \
    --userns=keep-id:uid=10001,gid=10001 --user 10001:10001 --read-only \
    --cap-drop=all --security-opt=no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m \
    --mount "type=bind,src=$temporary_dir,dst=/verify,rw" \
    --secret "$AUDIT_SECRET,type=mount,target=audit-key,uid=10001,gid=10001,mode=0400" \
    --entrypoint /opt/venv/bin/python "$image" -m stram_quant_mcp.audit verify \
    --db /verify/audit.sqlite3 --key-file /run/secrets/audit-key >/dev/null || status=$?
  rm -rf "$temporary_dir"
  [ "$status" = 0 ] || fail 'Independent audit HMAC chain verification failed.'
  pass 'independent checkpoint signature, backup digest and audit HMAC chain verification passed'
}

install_backup_timer() {
  require_root
  cat > "$BACKUP_SERVICE" <<EOF
[Unit]
Description=Stram Quant MCP consistent audit backup and checkpoint
After=stram-quant-egress.service

[Service]
Type=oneshot
ExecStart=/usr/bin/bash $REPO_ROOT/deploy/stram_quant_siddu.sh backup-audit
EOF
  cat > "$BACKUP_TIMER" <<'EOF'
[Unit]
Description=Daily Stram Quant MCP audit backup

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
EOF
  chmod 0644 "$BACKUP_SERVICE" "$BACKUP_TIMER"
  chown root:root "$BACKUP_SERVICE" "$BACKUP_TIMER"
  systemctl daemon-reload
  systemctl enable --now stram-quant-audit-backup.timer
  systemctl is-enabled --quiet stram-quant-audit-backup.timer || fail 'Audit backup timer is not persistent.'
  pass 'daily persistent audit backup timer and 30-day staging retention are configured'
}

rollback() {
  require_root
  [ "$(env_value "$APPROVAL" ROLLBACK_AUTHORIZED)" = yes ] || fail 'ROLLBACK_AUTHORIZED is not yes.'
  local rollback_image temporary
  rollback_image=$(env_value "$APPROVAL" ROLLBACK_IMAGE_REF)
  digest_ref "$rollback_image" || fail 'Rollback image is not an immutable digest reference.'
  as_quant podman image exists "$rollback_image" || fail 'Rollback image digest is not retained locally.'
  backup_audit
  probe_quant "$rollback_image"
  temporary=$(mktemp)
  render_quant_quadlet "$rollback_image" "$temporary"
  cp --preserve=mode,ownership,timestamps "$QUANT_QUADLET" "$QUANT_QUADLET.pre-rollback"
  install -o "$QUANT_USER" -g "$QUANT_USER" -m 0600 "$temporary" "$QUANT_QUADLET"
  rm -f "$temporary"
  as_quant systemctl --user daemon-reload
  as_quant systemctl --user restart stram-quant-mcp.service
  wait_for_mcp_readiness 30 || fail 'Rollback readiness failed.'
  pass 'previous digest restored readiness; audit backup/checkpoint preserved and the Data Plane was untouched'
}

restore_approved() {
  require_root
  local approved_image temporary
  approved_image=$(env_value "$APPROVAL" APPROVED_IMAGE_REF)
  digest_ref "$approved_image" || fail 'Approved image is not an immutable digest reference.'
  as_quant podman image exists "$approved_image" || fail 'Approved image digest is not retained locally.'
  probe_quant "$approved_image"
  temporary=$(mktemp)
  render_quant_quadlet "$approved_image" "$temporary"
  cp --preserve=mode,ownership,timestamps "$QUANT_QUADLET" "$QUANT_QUADLET.rollback-drill"
  install -o "$QUANT_USER" -g "$QUANT_USER" -m 0600 "$temporary" "$QUANT_QUADLET"
  rm -f "$temporary"
  as_quant systemctl --user daemon-reload
  as_quant systemctl --user restart stram-quant-mcp.service
  wait_for_mcp_readiness 30 || fail 'Approved-image recovery readiness failed.'
  pass 'approved digest recovered after the rollback drill; audit volume was preserved'
}

enable_services() {
  require_root
  [ "$(env_value "$APPROVAL" AUDIT_BACKUP_CHECKPOINT_RETENTION_VERIFIED)" = yes ] || fail 'Audit backup/checkpoint evidence is not approved.'
  [ "$(env_value "$APPROVAL" ROLLBACK_IMAGE_AND_DRILL_VERIFIED)" = yes ] || fail 'Rollback/recovery drill evidence is not approved.'
  verify
  install -o root -g root -m 0400 /dev/null "$PERSISTENCE_MARKER"
  auth_values
  render_auth_quadlets
  local approved_image temporary
  approved_image=$(env_value "$APPROVAL" APPROVED_IMAGE_REF)
  temporary=$(mktemp)
  render_quant_quadlet "$approved_image" "$temporary"
  install -o "$QUANT_USER" -g "$QUANT_USER" -m 0600 "$temporary" "$QUANT_QUADLET"
  rm -f "$temporary"
  as_auth systemctl --user daemon-reload
  as_quant systemctl --user daemon-reload
  as_auth systemctl --user is-enabled stram-keycloak-db.service stram-keycloak.service >/dev/null || fail 'Auth services are not persistent.'
  as_quant systemctl --user is-enabled stram-quant-mcp.service >/dev/null || fail 'Quant service is not persistent.'
  pass 'verified rootless Keycloak, PostgreSQL and Quant MCP services persist across reboot'
}

status() {
  printf 'UTC: %s\n' "$(date -u +%FT%TZ)"
  if [ "$(id -u)" = 0 ] && id "$AUTH_USER" >/dev/null 2>&1; then
    as_auth systemctl --user status stram-keycloak-db.service stram-keycloak.service --no-pager || true
  else
    not_verified 'Keycloak user-systemd status requires root and the stram-auth identity'
  fi
  if [ "$(id -u)" = 0 ] && id "$QUANT_USER" >/dev/null 2>&1; then
    as_quant systemctl --user status stram-quant-mcp.service --no-pager || true
  else
    not_verified 'Quant user-systemd status requires root'
  fi
  curl --max-time 3 --fail --silent http://127.0.0.1:8800/health/ready 2>/dev/null && printf '\n' || true
}

discovery_routes() {
  # RFC 9728/8414 discovery paths must be reachable on the same HTTPS origin as /mcp and /realms.
  # Usage: discovery-routes {status|apply|remove}. Mutation needs root plus explicit confirmation.
  local mode=${2:-status} resource_path realm_path
  require_command tailscale
  auth_values
  resource_path=$(python3 -c 'import sys,urllib.parse as u;print(u.urlsplit(sys.argv[1]).path)' "$MCP_RESOURCE_URL")
  realm_path=$(python3 -c 'import sys,urllib.parse as u;print(u.urlsplit(sys.argv[1]).path)' "$MCP_ISSUER_URL")
  local prm_path="/.well-known/oauth-protected-resource$resource_path"
  local asm_path="/.well-known/oauth-authorization-server$realm_path"
  case "$mode" in
    status)
      local serve_json
      serve_json=$(tailscale serve status --json 2>/dev/null) || { not_verified 'Tailscale Serve state is unreadable'; return 0; }
      for route in "$prm_path" "$asm_path"; do
        if printf '%s' "$serve_json" | grep -qF "\"$route\""; then pass "Serve route present: $route"; else printf 'FAIL: Serve route missing: %s\n' "$route"; fi
      done ;;
    apply)
      require_root
      [ "${STRAM_CONFIRM_DISCOVERY_ROUTES:-}" = yes ] || fail 'Set STRAM_CONFIRM_DISCOVERY_ROUTES=yes after reviewing the routes.'
      tailscale serve --bg --https=443 --set-path "$prm_path" "http://127.0.0.1:8800$prm_path"
      tailscale serve --bg --https=443 --set-path "$asm_path" "http://127.0.0.1:9080$realm_path/.well-known/oauth-authorization-server"
      pass 'discovery routes added; existing /mcp and /realms routes were not modified' ;;
    remove)
      require_root
      [ "${STRAM_CONFIRM_DISCOVERY_ROUTES:-}" = yes ] || fail 'Set STRAM_CONFIRM_DISCOVERY_ROUTES=yes to remove the routes.'
      tailscale serve --https=443 --set-path "$prm_path" off
      tailscale serve --https=443 --set-path "$asm_path" off
      pass 'discovery routes removed; existing /mcp and /realms routes were not modified' ;;
    *) fail 'discovery-routes accepts status, apply or remove.' ;;
  esac
}

case "$ACTION" in
  doctor) doctor ;;
  prepare) prepare ;;
  provision-auth) provision_auth ;;
  import-images) import_images ;;
  auth-prepare) auth_prepare ;;
  install-egress) install_egress ;;
  auth-start) auth_start ;;
  auth-verify) auth_verify ;;
  prepare-acceptance) prepare_acceptance ;;
  activate) activate ;;
  verify) verify ;;
  reliability-drill) reliability_drill ;;
  backup-audit) backup_audit ;;
  verify-backup) verify_backup ;;
  install-backup-timer) install_backup_timer ;;
  rollback) rollback ;;
  restore-approved) restore_approved ;;
  enable-services) enable_services ;;
  discovery-routes) discovery_routes "$@" ;;
  status) status ;;
  *)
    printf 'Usage: sudo bash %s {doctor|prepare|provision-auth|import-images|auth-prepare|install-egress|auth-start|auth-verify|prepare-acceptance|activate|verify|reliability-drill|backup-audit|verify-backup|install-backup-timer|rollback|restore-approved|enable-services|discovery-routes|status}\n' "$0" >&2
    exit 2
    ;;
esac
