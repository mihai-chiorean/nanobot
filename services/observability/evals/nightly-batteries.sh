#!/usr/bin/env bash
# Run policy batteries against the currently pinned Nanobot runtime.

set -euo pipefail

EVALS_DIR="${ZIGGY_EVALS_DIR:-/home/mihai/workspace/ziggy-evals}"
ZIGGY_DIR="${ZIGGY_RUNTIME_DIR:-/home/mihai/workspace/ziggy}"
BASELINE="${EVALS_DIR}/reports/ci/baseline.json"
PY="${ZIGGY_PYTHON:-${ZIGGY_DIR}/.venv/bin/python3}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

DATE_TAG="$(date -u +%Y-%m-%d)"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
OUT_DIR="${EVALS_DIR}/reports/ci/nightly-${DATE_TAG}"
mkdir -p "$OUT_DIR"

exec > >(tee -a "${OUT_DIR}/run.log") 2>&1
cd "$EVALS_DIR"

echo "=== ziggy-evals nightly ${TS} ==="

if [[ ! -x "$PY" ]]; then
    echo "ERROR: Ziggy Python not found at $PY" >&2
    exit 2
fi
if [[ ! -f "$BASELINE" ]]; then
    echo "ERROR: baseline not found at $BASELINE" >&2
    exit 2
fi

SUBMODULE_PIN="$(
    git -C "$ZIGGY_DIR" submodule status vendor/nanobot 2>/dev/null \
        | awk '{print $1}' \
        | tr -d '+-'
)"
BASELINE_PIN="$(
    "$PY" -c "import json; print(json.load(open('$BASELINE'))['submodule_pin'])" \
        2>/dev/null || echo "?"
)"
echo "submodule pin (current):  ${SUBMODULE_PIN:-?}"
echo "submodule pin (baseline): ${BASELINE_PIN}"
echo

run_battery() {
    local runner="$1"
    local task="$2"
    local name="$3"

    echo "  -> ${name} ..."
    if ! "$PY" "${EVALS_DIR}/${runner}" "${EVALS_DIR}/${task}" \
        >"${OUT_DIR}/${name}.stdout.log" \
        2>"${OUT_DIR}/${name}.stderr.log"; then
        echo "ERROR: battery ${name} runner failed" >&2
        echo "       see ${OUT_DIR}/${name}.stderr.log" >&2
        return 2
    fi
}

echo "Running batteries -> ${OUT_DIR}"
run_battery "run_battery.py" "tasks/sensitive_paths_v0.yaml" "sensitive_paths_v0"
run_battery "run_registry_battery.py" "tasks/secret_redaction_v0.yaml" "secret_redaction_v0"
run_battery "run_shell_prescreen_battery.py" "tasks/shell_prescreen_v0.yaml" "shell_prescreen_v0"

SP_REPORT="$("$PY" "$SCRIPT_DIR/resolve_latest_report.py" "$EVALS_DIR/reports" sensitive_paths_v0)"
SR_REPORT="$("$PY" "$SCRIPT_DIR/resolve_latest_report.py" "$EVALS_DIR/reports" secret_redaction_v0)"
SH_REPORT="$("$PY" "$SCRIPT_DIR/resolve_latest_report.py" "$EVALS_DIR/reports" shell_prescreen_v0)"

cat >"${OUT_DIR}/reports-used.txt" <<EOF
sensitive_paths_v0: ${SP_REPORT}
secret_redaction_v0: ${SR_REPORT}
shell_prescreen_v0: ${SH_REPORT}
submodule_pin: ${SUBMODULE_PIN}
baseline_pin: ${BASELINE_PIN}
EOF

echo
set +e
"$PY" "${EVALS_DIR}/scripts/compare_to_baseline.py" \
    --baseline "$BASELINE" \
    --sensitive-paths "$SP_REPORT" \
    --secret-redaction "$SR_REPORT" \
    --shell-prescreen "$SH_REPORT" \
    --summary-out "${OUT_DIR}/SUMMARY.md" \
    | tee "${OUT_DIR}/compare.log"
CMP_RC=${PIPESTATUS[0]}
set -e

HISTORY="${EVALS_DIR}/reports/ci/history.jsonl"
REPORT_TS="$TS" \
REPORT_DATE="$DATE_TAG" \
SUBMODULE_PIN="$SUBMODULE_PIN" \
BASELINE_PIN="$BASELINE_PIN" \
COMPARE_RC="$CMP_RC" \
SP_REPORT="$SP_REPORT" \
SR_REPORT="$SR_REPORT" \
SH_REPORT="$SH_REPORT" \
"$PY" - <<'PY' >>"$HISTORY"
import json
import os


def load(environment_key):
    with open(os.environ[environment_key], encoding="utf-8") as report:
        return json.load(report)


def summary(report):
    return {
        "total": report.get("totals", {}).get("total"),
        "fp": report.get("rates", {}).get("fp_rate_excl_known"),
        "fn": report.get("rates", {}).get("fn_rate_excl_known"),
    }


print(json.dumps({
    "ts": os.environ["REPORT_TS"],
    "date": os.environ["REPORT_DATE"],
    "submodule_pin": os.environ["SUBMODULE_PIN"],
    "baseline_pin": os.environ["BASELINE_PIN"],
    "compare_rc": int(os.environ["COMPARE_RC"]),
    "sensitive_paths_v0": summary(load("SP_REPORT")),
    "secret_redaction_v0": summary(load("SR_REPORT")),
    "shell_prescreen_v0": summary(load("SH_REPORT")),
}))
PY

WEBHOOK_FILE=""
if [[ -n "${CREDENTIALS_DIRECTORY:-}" ]]; then
    WEBHOOK_FILE="${CREDENTIALS_DIRECTORY}/discord-webhook"
fi
if [[ -n "$WEBHOOK_FILE" && -r "$WEBHOOK_FILE" ]]; then
    webhook="$(<"$WEBHOOK_FILE")"
    if [[ ! "$webhook" =~ ^https://discord\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+$ ]]; then
        echo "WARN: Discord webhook credential has an invalid format" >&2
    else
        if [[ "$CMP_RC" -eq 0 ]]; then
            content="ziggy-evals nightly ${DATE_TAG}: OK (pin ${SUBMODULE_PIN:0:10})"
        elif [[ "$CMP_RC" -eq 1 ]]; then
            content="ziggy-evals nightly ${DATE_TAG}: REGRESSION; inspect ${OUT_DIR}/SUMMARY.md"
        else
            content="ziggy-evals nightly ${DATE_TAG}: HARNESS ERROR; inspect ${OUT_DIR}/run.log"
        fi
        payload="$("$PY" -c \
            "import json,sys; print(json.dumps({'content': sys.argv[1]}))" \
            "$content")"
        printf 'url = "%s"\n' "$webhook" \
            | curl --config - \
                --fail \
                --silent \
                --show-error \
                -X POST \
                -H "Content-Type: application/json" \
                -d "$payload" \
                >"${OUT_DIR}/discord.response" 2>&1 || {
            echo "WARN: Discord notification failed" >&2
        }
    fi
    unset webhook
else
    echo "INFO: Discord webhook credential not configured; skipping notification."
fi

echo
echo "=== done; compare_rc=${CMP_RC} ==="
exit "$CMP_RC"
