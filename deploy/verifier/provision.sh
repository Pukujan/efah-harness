#!/usr/bin/env bash
# Provision the verifier service identity — DEC-006 option B.
#
# Contract EFAH-CONTRACT-001 v1.1 §17.2: "The protected verifier MUST be in a
# separate repository and/or service identity that implementation workers cannot
# read, list, clone, query, or modify." A separate service identity alone
# satisfies the clause, and the same pattern is already proven live for the
# protected model-identity store (a second TerminusDB container whose port the
# main admin credential receives 401 against).
#
# What this creates:
#
#   efah-verifier            a system account, no login shell, no home in /home
#   /var/lib/efah-verifier   0700, owned by that account — the builder cannot
#                            list it, and src/verifier_identity/identity.py
#                            proves that by attempting the read
#   /opt/efah-verifier/bin   0755 ROOT-owned — the verifier identity cannot
#                            rewrite its own generator, so compromising that
#                            account does not let it change what is generated
#   /etc/sudoers.d/efah-verifier
#                            the builder may run exactly the generator as the
#                            verifier identity, and nothing else
#
# HONEST LIMIT, stated here because it belongs next to the mechanism rather than
# in a document nobody opens with it: the builder runs as a user holding
# passwordless sudo and docker group membership. The narrow sudoers rule below
# is therefore an *audit and accident* boundary, not a security one — that user
# can already become root. DEC-006 records this; option A (sealed side on a
# separate host under an owner-held identity) remains the durable path. Do not
# let this script's thoroughness read as a claim it does not make.
#
# Idempotent. Safe to re-run.
#
# Usage:  sudo deploy/verifier/provision.sh [--eval-key-from PATH]

set -euo pipefail

VERIFIER_USER="efah-verifier"
VERIFIER_HOME="/var/lib/efah-verifier"
SEALED_STORE="${VERIFIER_HOME}/store"
VERIFIER_ETC="${VERIFIER_HOME}/etc"
VERIFIER_LOG="${VERIFIER_HOME}/log"
GENERATOR_DIR="/opt/efah-verifier/bin"
GENERATOR="${GENERATOR_DIR}/generate-holdouts"
SUDOERS="/etc/sudoers.d/efah-verifier"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILDER_USER="${SUDO_USER:-$(id -un)}"
EVAL_KEY_SOURCE=""
PYTHON_BIN="${EFAH_VERIFIER_PYTHON:-/usr/bin/python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --eval-key-from) EVAL_KEY_SOURCE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "provision.sh must run as root (it creates a system account)" >&2
  exit 1
fi
if [[ "${BUILDER_USER}" == "root" ]]; then
  echo "refusing to run with SUDO_USER unset: the sudoers rule needs the real builder user" >&2
  exit 1
fi

echo "== verifier service identity =================================="
echo "builder identity : ${BUILDER_USER}"
echo "verifier identity: ${VERIFIER_USER}"

# -- 1. the account ----------------------------------------------------------
# --system: no mail spool, uid below 1000, excluded from ordinary user listings.
# nologin: the account exists to own files and run one program under sudo, and
# an interactive shell on it would be a second way in.
if ! id -u "${VERIFIER_USER}" >/dev/null 2>&1; then
  useradd --system \
          --home-dir "${VERIFIER_HOME}" \
          --shell /usr/sbin/nologin \
          --comment "EFAH protected verifier (DEC-006 option B)" \
          "${VERIFIER_USER}"
  echo "created ${VERIFIER_USER}"
else
  echo "${VERIFIER_USER} already exists"
fi

# The builder must not be able to reach the store through group membership
# either. A 0700 directory owned by a user whose group the builder is in would
# still be closed, but leaving the builder in that group is an invitation.
if id -nG "${BUILDER_USER}" | tr ' ' '\n' | grep -qx "${VERIFIER_USER}"; then
  echo "REFUSING: ${BUILDER_USER} is a member of group ${VERIFIER_USER}" >&2
  echo "remove it before provisioning: gpasswd -d ${BUILDER_USER} ${VERIFIER_USER}" >&2
  exit 1
fi

# -- 2. the store ------------------------------------------------------------
install -d -m 0700 -o "${VERIFIER_USER}" -g "${VERIFIER_USER}" "${VERIFIER_HOME}"
install -d -m 0700 -o "${VERIFIER_USER}" -g "${VERIFIER_USER}" "${SEALED_STORE}"
install -d -m 0700 -o "${VERIFIER_USER}" -g "${VERIFIER_USER}" "${VERIFIER_ETC}"
install -d -m 0700 -o "${VERIFIER_USER}" -g "${VERIFIER_USER}" "${VERIFIER_LOG}"
# Frozen exams, one directory per exam named for its own content hash. Created
# here so re-provisioning never has to remove it: a mint writes a new exam and
# never rewrites an old one, and a pin recorded in evidence/sealed-exam-pin.json
# must keep resolving across provisioning runs or every recorded verdict loses
# the thing it was bound to.
install -d -m 0700 -o "${VERIFIER_USER}" -g "${VERIFIER_USER}" "${SEALED_STORE}/exams"
echo "store: ${SEALED_STORE} (0700, ${VERIFIER_USER}); exams preserved"

# -- 3. the generator --------------------------------------------------------
# Root-owned and not writable by the verifier identity. The account that runs
# the generator cannot modify it, so a compromise of that account cannot
# silently change what is generated or where it is written.
install -d -m 0755 -o root -g root "${GENERATOR_DIR}"
install -m 0755 -o root -g root "${REPO_ROOT}/deploy/verifier/generator.py" "${GENERATOR}"
echo "generator: ${GENERATOR} (0755, root — not writable by ${VERIFIER_USER})"

# -- 4. the credential -------------------------------------------------------
# The verifier identity gets its own copy of the eval-gateway key so the two
# sides can diverge later (DEC-006 option A) without touching any code. This is
# not a new secret: the builder already holds this key. What is being separated
# is the *output*, not the credential.
if [[ -n "${EVAL_KEY_SOURCE}" ]]; then
  if [[ ! -f "${EVAL_KEY_SOURCE}" ]]; then
    echo "eval key source not found: ${EVAL_KEY_SOURCE}" >&2
    exit 1
  fi
  umask 077
  grep -E '^LITELLM_EVAL_MASTER_KEY=' "${EVAL_KEY_SOURCE}" > "${VERIFIER_ETC}/eval.env" || {
    echo "no LITELLM_EVAL_MASTER_KEY in ${EVAL_KEY_SOURCE}" >&2
    exit 1
  }
  chown "${VERIFIER_USER}:${VERIFIER_USER}" "${VERIFIER_ETC}/eval.env"
  chmod 0600 "${VERIFIER_ETC}/eval.env"
  echo "credential: ${VERIFIER_ETC}/eval.env (0600, ${VERIFIER_USER})"
else
  echo "credential: not installed (pass --eval-key-from PATH); generation will"
  echo "            report MISSING_REQUIRED_CREDENTIAL until it is"
fi

# -- 5. the sudoers rule -----------------------------------------------------
# Narrow on purpose. See the honest limit at the top of this file: ${BUILDER_USER}
# holds blanket sudo, so this restricts the *sanctioned path*, which is what
# makes out-of-band access visible in the audit log rather than indistinguishable
# from normal operation.
cat > "${SUDOERS}.tmp" <<EOF
# EFAH verifier service identity — DEC-006 option B. Managed by
# deploy/verifier/provision.sh; edit that, not this.
#
# The builder may enter the verifier identity for exactly one program. It may
# NOT read the store, list it, or run a shell as ${VERIFIER_USER}.
Defaults:${BUILDER_USER} !requiretty
${BUILDER_USER} ALL=(${VERIFIER_USER}) NOPASSWD: ${GENERATOR}
EOF

# visudo -c on a temp file, then move. A malformed sudoers file locks the host
# out of sudo entirely, and this script runs unattended.
if visudo -c -q -f "${SUDOERS}.tmp"; then
  install -m 0440 -o root -g root "${SUDOERS}.tmp" "${SUDOERS}"
  rm -f "${SUDOERS}.tmp"
  echo "sudoers: ${SUDOERS} (0440) — ${BUILDER_USER} may run only the generator"
else
  rm -f "${SUDOERS}.tmp"
  echo "REFUSING: generated sudoers fragment failed visudo validation" >&2
  exit 1
fi

# -- 5b. the shared throttle -------------------------------------------------
# The upstream rate limit is ACCOUNT-WIDE, so the builder and the verifier must
# share one limiter. Two identities each pacing to 90 rpm emit 180 and
# self-inflict 429s indistinguishable from genuine model failure. The verifier
# hitting a builder-owned file in /tmp is how that was found: PermissionError,
# mid-generation, after the model calls had already been paid for.
#
# The file holds a single float — the next permissible dispatch instant. Mode
# 0666 rather than 0660 because a group added now does not apply to sessions
# already running, and a throttle one side cannot write is a throttle that
# silently stops throttling. The directory stays root-owned so neither identity
# can replace the file.
groupadd -f efah-throttle
usermod -aG efah-throttle "${VERIFIER_USER}"
usermod -aG efah-throttle "${BUILDER_USER}"
install -d -m 0755 -o root -g efah-throttle /var/lib/efah-throttle
[ -f /var/lib/efah-throttle/state.json ] || echo '{}' > /var/lib/efah-throttle/state.json
chown root:efah-throttle /var/lib/efah-throttle/state.json
chmod 0666 /var/lib/efah-throttle/state.json
echo "throttle: /var/lib/efah-throttle/state.json (shared, account-wide limit)"

# -- 5c. a test runner the verifier can execute and cannot modify ------------
# The generator's mutation gate is worthless without pytest, and the failure is
# silent in the worst way: `python -m pytest` with no pytest exits 1, exactly
# like a failing test. That collision produced a kill_rate of 1.0 on a holdout
# set that ran no tests at all — every mutant "died" of the runner being absent.
# Root-owned for the same reason the generator is: the account that runs the
# tests must not be able to change the test runner.
"${PYTHON_BIN}" -m venv /opt/efah-verifier/venv
/opt/efah-verifier/venv/bin/pip install --quiet --disable-pip-version-check pytest
chown -R root:root /opt/efah-verifier/venv
chmod -R go-w /opt/efah-verifier/venv
echo "test runner: $(/opt/efah-verifier/venv/bin/python -m pytest --version 2>&1)"

# -- 6. the python the generator runs under ---------------------------------
if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "REFUSING: ${PYTHON_BIN} is not executable; set EFAH_VERIFIER_PYTHON" >&2
  exit 1
fi
# The builder's virtualenv lives under its home and the verifier identity must
# not depend on anything the builder can rewrite. A system interpreter is the
# only one neither side can quietly change.
case "${PYTHON_BIN}" in
  /home/*) echo "REFUSING: ${PYTHON_BIN} is under a builder-writable home" >&2; exit 1 ;;
esac
install -m 0644 -o root -g root /dev/null "${GENERATOR_DIR}/interpreter"
echo "${PYTHON_BIN}" > "${GENERATOR_DIR}/interpreter"
echo "interpreter: ${PYTHON_BIN}"

echo
echo "== provisioned. verify from the BUILDER identity, not from root: ======"
echo "  PYTHONPATH=src python -m verifier_identity.identity  # or:"
echo "  PYTHONPATH=src python tools/gate_dec_006.py"
