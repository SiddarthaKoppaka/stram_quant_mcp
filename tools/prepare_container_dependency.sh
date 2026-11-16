#!/bin/sh
set -eu

expected_commit=2eaf9aef309e3be3ed6d575cbc84d7813b1ea6be
source_path=${1:-../stram_data}
archive_path=$(mktemp -d)
trap 'rm -rf "$archive_path"' EXIT

# Release 1.12.0 is not tagged; prove the pinned commit declares that version instead.
test "$(git -C "$source_path" rev-parse "$expected_commit^{commit}")" = "$expected_commit"
git -C "$source_path" show "$expected_commit:pyproject.toml" | grep -q '^version = "1.12.0"$'
git -C "$source_path" archive "$expected_commit" | tar -x -C "$archive_path"
install -d -m 0755 vendor
uv build --wheel --out-dir vendor "$archive_path"
test -f vendor/stram_data-1.12.0-py3-none-any.whl
sha256sum vendor/stram_data-1.12.0-py3-none-any.whl
