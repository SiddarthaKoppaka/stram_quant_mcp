#!/bin/sh
set -eu

expected_commit=13cd0913fafcc31115bd7cab8b91fa3858d902d0
source_path=${1:-../stram_data}
archive_path=$(mktemp -d)
trap 'rm -rf "$archive_path"' EXIT

# Release 1.10.0 is not tagged; prove the pinned commit declares that version instead.
test "$(git -C "$source_path" rev-parse "$expected_commit^{commit}")" = "$expected_commit"
git -C "$source_path" show "$expected_commit:pyproject.toml" | grep -q '^version = "1.10.0"$'
git -C "$source_path" archive "$expected_commit" | tar -x -C "$archive_path"
install -d -m 0755 vendor
uv build --wheel --out-dir vendor "$archive_path"
test -f vendor/stram_data-1.10.0-py3-none-any.whl
sha256sum vendor/stram_data-1.10.0-py3-none-any.whl
