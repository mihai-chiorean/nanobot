# Ziggy Nightly Evaluations

This directory owns the scheduled policy-battery wrapper used on Spark. The
evaluation datasets and generated reports remain in the separate
`/home/mihai/workspace/ziggy-evals` data tree.

The wrapper deliberately avoids `ls | head` report selection. Under
`set -o pipefail`, that pipeline eventually exits with SIGPIPE as report count
grows and makes a successful battery run look like a harness failure.

## Verify

```sh
python3 -m unittest discover \
  -s services/observability/evals \
  -p 'test_*.py'
bash -n services/observability/evals/nightly-batteries.sh
```

## Install

Install the wrapper, helper, service, and timer from a reviewed release:

```sh
install -d -m 0755 /home/mihai/.local/libexec/ziggy-evals
install -m 0755 \
  services/observability/evals/nightly-batteries.sh \
  services/observability/evals/resolve_latest_report.py \
  /home/mihai/.local/libexec/ziggy-evals/
install -m 0644 \
  services/observability/evals/systemd/ziggy-eval-nightly.service \
  services/observability/evals/systemd/ziggy-eval-nightly.timer \
  /home/mihai/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ziggy-eval-nightly.timer
systemctl --user start ziggy-eval-nightly.service
```

Discord notification is optional. Store a fresh webhook URL at
`~/.config/credstore/ziggy-eval-discord-webhook` with mode `0600`, install the
example drop-in as `credential.conf`, and reload the user manager. The webhook
must never be placed in an `Environment=` directive because `systemctl cat`
and process inspection can expose it.
