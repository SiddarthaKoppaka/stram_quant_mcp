#!/bin/sh
set -eu

expected_commit=890b44911b56d0f165cfa7962032d00292b4e10e
source_path=${1:-../stram_data}
archive_path=$(mktemp -d)
trap 'rm -rf "$archive_path"' EXIT

test "$(git -C "$source_path" rev-parse 'v1.3.1^{commit}')" = "$expected_commit"
git -C "$source_path" archive "$expected_commit" | tar -x -C "$archive_path"
install -d -m 0755 vendor
uv build --wheel --out-dir vendor "$archive_path"
test -f vendor/stram_data-1.3.1-py3-none-any.whl
sha256sum vendor/stram_data-1.3.1-py3-none-any.whl
