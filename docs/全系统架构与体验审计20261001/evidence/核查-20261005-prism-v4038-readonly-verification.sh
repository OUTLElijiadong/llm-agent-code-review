#!/usr/bin/env bash
set -Eeuo pipefail
sha="$1"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
cd "/opt/prism-releases/$sha/deploy"
source lib/common.sh
load_release_environment .releases/current.env
[[ "$APP_RELEASE" == "$sha" && "$APP_VERSION" == 4.0.38 ]]
[[ "$(git -C .. rev-parse HEAD)" == "$APP_RELEASE" ]]
git -C .. diff --quiet HEAD
assert_deploy_sources_clean ..
printf 'RELEASE %s VERSION %s\n' "$APP_RELEASE" "$APP_VERSION"
for name in cr_backend cr_frontend; do
  docker inspect --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.Health.Status}}' "$name"
done
printf 'ALEMBIC %s\n' "$(current_alembic_revision)"
backup_file="$(read_release_value .releases/current.env BACKUP_FILE)"
backup_file="$(readlink -f -- "$backup_file")"
[[ "$backup_file" == /opt/prism-releases/$sha/backups/*.sql.gz ]]
gzip -t "$backup_file"
sha256sum "$backup_file"
stat -c 'BACKUP_BYTES %s' "$backup_file"
docker exec -i cr_backend python - <<'PY'
import hashlib,importlib.metadata as m,json,pathlib,re
lock=pathlib.Path('/app/requirements.lock')
pinned=dict(re.findall(r'^([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;]+)',lock.read_text(),re.M))
mismatch=[name for name,version in pinned.items() if m.version(name)!=version]
result={'locked_application_packages':len(pinned),'version_mismatches':mismatch,'pyjwt':m.version('PyJWT'),'lock_sha256':hashlib.sha256(lock.read_bytes()).hexdigest()}
print(json.dumps(result,sort_keys=True))
assert not mismatch
PY
docker exec cr_backend python -m pip check
printf 'TRANSCRIPT_COUNTS_AND_DIGEST_MISMATCHES\n'
checkpoint_database_query 'SELECT COUNT(*), SUM(message_sha256 <> SHA2(message_json,256)) FROM agent_response_transcript_message'
before_backend="$(docker inspect --format '{{.Id}}' cr_backend)"
before_frontend="$(docker inspect --format '{{.Id}}' cr_frontend)"
assert_checkpoint_rollback_compatible
assert_project_member_rollback_compatible
set +e
(
  BOUND_BACKEND_IMAGE_ID="$(release_image_id prism-backend:732f48f6929ceda1585bcae2489a0b01f0f2c5fb)"
  assert_project_member_rollback_compatible
)
old_rc=$?
set -e
[[ "$old_rc" == 1 ]]
[[ "$before_backend" == "$(docker inspect --format '{{.Id}}' cr_backend)" ]]
[[ "$before_frontend" == "$(docker inspect --format '{{.Id}}' cr_frontend)" ]]
printf 'OLD37_MEMBER_READER_REJECTED %s CONTAINERS_UNCHANGED yes\n' "$old_rc"
./ops-check.sh
systemctl show prism-ops-executor.service --property=WorkingDirectory --property=ActiveState --property=SubState
df -B1 --output=used,avail,pcent /
