#!/usr/bin/env bash
# Usage: packaging/winget/new-version.sh 2.8.0
# Copies the newest manifest folder to a new version, downloads the release installer from GitHub, and
# fills in the version, URLs, release date and SHA-256. Then validate with `winget validate --manifest <dir>`.
set -euo pipefail
new="${1:?usage: new-version.sh <version, e.g. 2.8.0>}"
here="$(cd "$(dirname "$0")" && pwd)"
base="$here/manifests/a/AhmedRagab/FreeVideoEditor"
old="$(ls "$base" | sort -V | tail -n 1)"
[ "$old" != "$new" ] || { echo "Version $new already exists"; exit 1; }
url="https://github.com/Ahmed41022/free-video-editor/releases/download/v$new/FreeVideoEditor-Setup-$new.exe"
sha="$(curl -fsSL "$url" | sha256sum | cut -d' ' -f1 | tr a-f A-F)"
[ "${#sha}" -eq 64 ] || { echo "Could not hash $url"; exit 1; }
cp -r "$base/$old" "$base/$new"
cd "$base/$new"
for f in AhmedRagab.FreeVideoEditor*.yaml; do
  mv "$f" "$f.tmp"; sed "s/${old//./\.}/$new/g" "$f.tmp" > "$f"; rm "$f.tmp"
done
sed -i "s/^\( *InstallerSha256:\).*/\1 $sha/" AhmedRagab.FreeVideoEditor.installer.yaml
sed -i "s/^ReleaseDate:.*/ReleaseDate: $(date -u +%F)/" AhmedRagab.FreeVideoEditor.installer.yaml
echo "Created $base/$new (SHA-256 $sha)"
