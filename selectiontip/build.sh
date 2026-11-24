#!/usr/bin/env bash
# Builds selectiontip.oxt in ../dist
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p ../dist
out="$(realpath ../dist)/selectiontip.oxt"
rm -f "$out"
python3 - "$out" <<'EOF'
import sys, zipfile
files = ["description.xml", "description-pt-BR.txt", "META-INF/manifest.xml",
         "Jobs.xcu", "python/selectiontip.py"]
with zipfile.ZipFile(sys.argv[1], "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(f, f)
EOF
echo "built: $out"
