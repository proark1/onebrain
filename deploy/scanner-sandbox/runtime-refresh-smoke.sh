#!/bin/sh
set -eu

# This is an explicitly networked integration probe. It must never be copied
# into, or executed by, the production final-image dependency graph.
launcher=/usr/local/bin/onebrain-scanner-sandbox
baseline=/opt/onebrain/clamav-baseline
update_target=/var/lib/onebrain/clamav/incoming/runtime-refresh-smoke

rm -rf "${update_target}"
mkdir -p "${update_target}"
cp "${baseline}"/*.cvd "${update_target}/" 2>/dev/null || true
cp "${baseline}"/*.cld "${update_target}/" 2>/dev/null || true
timeout --signal=TERM --kill-after=15s 300s \
    "${launcher}" definitions-update "${update_target}"
for database in "${update_target}"/*.cvd "${update_target}"/*.cld; do
    test ! -f "${database}" || sigtool --info "${database}" >/dev/null
done
rm -rf "${update_target}"
