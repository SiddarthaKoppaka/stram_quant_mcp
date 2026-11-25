#!/bin/sh
set -eu

expected_commit=f4396f5d8e4c65016a71c6db8c39bb8e72acb80a
source_path=${1:-../stram_data}
archive_path=$(mktemp -d)
trap 'rm -rf "$archive_path"' EXIT

# Release 1.16.0 is not tagged; prove the pinned commit declares that version instead.
test "$(git -C "$source_path" rev-parse "$expected_commit^{commit}")" = "$expected_commit"
git -C "$source_path" show "$expected_commit:pyproject.toml" | grep -q '^version = "1.16.0"$'
git -C "$source_path" archive "$expected_commit" | tar -x -C "$archive_path"
install -d -m 0755 vendor
uv build --wheel --out-dir vendor "$archive_path"
test -f vendor/stram_data-1.16.0-py3-none-any.whl
sha256sum vendor/stram_data-1.16.0-py3-none-any.whl
