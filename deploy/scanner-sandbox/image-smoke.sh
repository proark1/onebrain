#!/bin/sh
set -eu

launcher=/usr/local/bin/onebrain-scanner-sandbox
definitions=/var/lib/onebrain/clamav/sets/image-baseline
fixtures=/opt/onebrain/scanner-fixtures
scratch=/tmp/onebrain/scanner-image-smoke
mkdir -p "${scratch}"

scan_file() {
    input=$1
    output=$2
    max_scan=$3
    max_file=$4
    max_files=$5
    max_recursion=$6
    max_time=$7
    bytecode_time=$8
    "${launcher}" scan \
        "--database=${definitions}" \
        --official-db-only=yes \
        --stdout \
        --no-summary \
        --scan-archive=yes \
        --alert-exceeds-max=yes \
        --alert-encrypted=yes \
        "--max-scansize=${max_scan}" \
        "--max-filesize=${max_file}" \
        "--max-files=${max_files}" \
        "--max-recursion=${max_recursion}" \
        "--max-scantime=${max_time}" \
        "--bytecode-timeout=${bytecode_time}" \
        - <"${input}" >"${output}"
}

expect_non_clean() {
    label=$1
    input=$2
    max_scan=$3
    max_file=$4
    max_files=$5
    max_recursion=$6
    max_time=$7
    bytecode_time=$8
    output="${scratch}/${label}.out"
    set +e
    scan_file "${input}" "${output}" "${max_scan}" "${max_file}" \
        "${max_files}" "${max_recursion}" "${max_time}" "${bytecode_time}"
    status=$?
    set -e
    test "${status}" -eq 1
    grep -Eiq '^stdin: .*FOUND$' "${output}"
}

expect_diagnostic() {
    label=$1
    input=$2
    expected=$3
    max_scan=$4
    max_file=$5
    max_files=$6
    max_recursion=$7
    max_time=$8
    bytecode_time=$9
    output="${scratch}/${label}.out"
    set +e
    scan_file "${input}" "${output}" "${max_scan}" "${max_file}" \
        "${max_files}" "${max_recursion}" "${max_time}" "${bytecode_time}"
    status=$?
    set -e
    test "${status}" -eq 1
    grep -Eiq "^stdin: ${expected} FOUND$" "${output}"
}

python -m app.drive.malware.definitions verify-release-evidence \
    /opt/onebrain/clamav-baseline \
    --evidence /opt/onebrain/scanner-release.json \
    --launcher /usr/local/bin/onebrain-scanner-sandbox \
    --clamav-binary /usr/bin/clamscan \
    --capabilities /opt/onebrain/scanner-capabilities.json \
    --packages /opt/onebrain/scanner-packages.txt \
    --supply-chain /opt/onebrain/worker-supply-chain.json >/dev/null

python /opt/onebrain/worker_supply_chain.py verify-lock \
    /opt/onebrain/worker-supply-chain.lock.json >/dev/null
python /opt/onebrain/worker_supply_chain.py verify-evidence \
    /opt/onebrain/worker-supply-chain.lock.json \
    /opt/onebrain/worker-supply-chain.json \
    --inventory /opt/onebrain/scanner-packages.txt >/dev/null

printf 'ordinary OneBrain scanner image fixture\n' >"${scratch}/clean.txt"
scan_file "${scratch}/clean.txt" "${scratch}/clean.out" \
    536870912 104857600 10000 16 45000 5000
grep -Eq '^stdin: (OK|Empty file)$' "${scratch}/clean.out"

printf '%s%s%s' 'X5O!P%@AP[4\PZX54(P^)7CC)7}$' \
    'EICAR-STANDARD-ANTIVIRUS-TEST-FILE!' '$H+H*' >"${scratch}/eicar.txt"
expect_non_clean eicar "${scratch}/eicar.txt" 536870912 104857600 10000 16 45000 5000
expect_diagnostic encrypted "${fixtures}/encrypted.zip" 'Heuristics\.Encrypted\..*' \
    536870912 104857600 10000 16 45000 5000
expect_diagnostic recursion "${fixtures}/recursion.zip" \
    'Heuristics\.Limits\.Exceeded\.MaxRecursion' \
    536870912 104857600 10000 2 45000 5000
expect_diagnostic member-count "${fixtures}/member-count.zip" \
    'Heuristics\.Limits\.Exceeded\.MaxFiles' \
    536870912 104857600 3 16 45000 5000
expect_diagnostic file-size "${fixtures}/file-size.bin" \
    'Heuristics\.Limits\.Exceeded\.MaxFileSize' \
    536870912 1024 10000 16 45000 5000
expect_diagnostic scan-size "${fixtures}/scan-size.zip" \
    'Heuristics\.Limits\.Exceeded\.MaxScanSize' \
    4096 104857600 10000 16 45000 5000
expect_diagnostic scan-time "${fixtures}/scan-time.zip" \
    'Heuristics\.Limits\.Exceeded\.MaxScanTime' \
    536870912 104857600 10000 16 1 5000

rm -rf "${scratch}"
