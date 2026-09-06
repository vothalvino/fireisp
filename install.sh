#!/usr/bin/env bash
# FireISP's small bootstrap. The selected release supplies the installation wizard.
set -Eeuo pipefail

FIREISP_REPOSITORY='https://github.com/vothalvino/fireisp.git'
FIREISP_INSTALLER_ROOT='/opt/fireisp/installer'
FIREISP_BOOTSTRAP_TEMP=''
FIREISP_SELECTED_RELEASE=''
FIREISP_WIZARD_ARGUMENTS=()

bootstrap_error() {
    printf 'FireISP: %s\n' "$*" >&2
    return 1
}

bootstrap_help() {
    cat <<'HELP'
FireISP interactive installer — Ubuntu 24.04

Run on the server you want to install:
  curl -fsSL https://raw.githubusercontent.com/vothalvino/fireisp/main/install.sh | sudo bash

Replay a particular release:
  curl -fsSL https://raw.githubusercontent.com/vothalvino/fireisp/main/install.sh | sudo bash -s -- --release FULL_GIT_SHA

The wizard lets you install the main server or connect additional module servers.
Bootstrap options:
  --release FULL_GIT_SHA   Use this exact 40-character Git commit instead of main.
  --help                  Show this help without changing the server.
  --                      Forward all subsequent arguments to the wizard.

Other arguments are forwarded unchanged to deploy/wizard.py. A terminal is required
for the interactive wizard, including when the bootstrap is piped into bash.
HELP
}

bootstrap_parse_arguments() {
    local requested=0
    while (($#)); do
        case "$1" in
            --help|-h)
                bootstrap_help
                return 2
                ;;
            --release)
                (($# >= 2)) || { bootstrap_error '--release needs a full Git commit.'; return 1; }
                FIREISP_SELECTED_RELEASE="$2"
                requested=1
                shift 2
                ;;
            --release=*)
                FIREISP_SELECTED_RELEASE="${1#*=}"
                requested=1
                shift
                ;;
            --)
                shift
                FIREISP_WIZARD_ARGUMENTS+=("$@")
                break
                ;;
            *)
                FIREISP_WIZARD_ARGUMENTS+=("$1")
                shift
                ;;
        esac
    done
    if ((requested)) && [[ ! "$FIREISP_SELECTED_RELEASE" =~ ^[0-9a-fA-F]{40}$ ]]; then
        bootstrap_error '--release must be a full 40-character Git commit.'
        return 1
    fi
    FIREISP_SELECTED_RELEASE="${FIREISP_SELECTED_RELEASE,,}"
}

bootstrap_secure_directory() {
    local directory="$1" mode="${2:-700}" owner permissions
    [[ ! -L "$directory" ]] || { bootstrap_error "Refusing symbolic-link directory: $directory"; return 1; }
    if [[ ! -e "$directory" ]]; then
        mkdir -m "$mode" -- "$directory"
    fi
    [[ -d "$directory" ]] || { bootstrap_error "Not a directory: $directory"; return 1; }
    owner="$(stat -c '%u' -- "$directory")"
    permissions="$(stat -c '%a' -- "$directory")"
    if [[ "$owner" != 0 ]] || (( (8#$permissions & 8#022) != 0 )); then
        bootstrap_error "Directory must be owned by root and not writable by other users: $directory"
        return 1
    fi
}

bootstrap_dependencies() {
    local -a missing=()
    local package
    for package in curl git python3; do
        command -v "$package" >/dev/null 2>&1 || missing+=("$package")
    done
    if [[ "$(dpkg-query -W -f='${Status}' ca-certificates 2>/dev/null || true)" != 'install ok installed' ]]; then
        missing+=(ca-certificates)
    fi
    if ((${#missing[@]})); then
        printf 'Installing bootstrap dependencies: %s\n' "${missing[*]}"
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
    fi
}

bootstrap_resolve_release() {
    local response reference extra attempt
    if [[ -n "$FIREISP_SELECTED_RELEASE" ]]; then
        return
    fi
    for attempt in 1 2 3; do
        if response="$(git ls-remote --exit-code "$FIREISP_REPOSITORY" refs/heads/main)"; then
            read -r FIREISP_SELECTED_RELEASE reference extra <<< "$response"
            if [[ "$FIREISP_SELECTED_RELEASE" =~ ^[0-9a-f]{40}$ && "$reference" == refs/heads/main && -z "$extra" && "$response" != *$'\n'* ]]; then
                return
            fi
            bootstrap_error 'GitHub returned an invalid release reference.'
            return 1
        fi
        if ((attempt < 3)); then sleep "$attempt"; fi
    done
    bootstrap_error 'Cannot resolve the FireISP release. No installer was executed.'
}

bootstrap_verify_checkout() {
    local directory="$1" release="$2" actual dirty
    [[ -d "$directory" && ! -L "$directory" && -d "$directory/.git" && ! -L "$directory/.git" ]] || {
        bootstrap_error 'The cached release is not a regular Git checkout.'; return 1;
    }
    actual="$(git -C "$directory" rev-parse --verify HEAD)"
    [[ "$actual" == "$release" ]] || { bootstrap_error 'The downloaded commit does not match the requested release.'; return 1; }
    dirty="$(git --no-optional-locks -C "$directory" status --porcelain --untracked-files=all --ignored)"
    [[ -z "$dirty" ]] || { bootstrap_error 'The cached release was modified; refusing to execute it.'; return 1; }
    [[ -f "$directory/deploy/wizard.py" && ! -L "$directory/deploy/wizard.py" ]] || {
        bootstrap_error 'This release does not contain the FireISP installation wizard.'; return 1;
    }
}

bootstrap_checkout() {
    local release="$1" destination="$FIREISP_INSTALLER_ROOT/releases/$1" attempt fetched=0
    if [[ -e "$destination" || -L "$destination" ]]; then
        bootstrap_secure_directory "$destination"
        if [[ -n "$(find "$destination" \( -perm /222 -o ! -uid 0 \) -print -quit)" ]]; then
            bootstrap_error 'The cached release must remain read-only and owned by root.'
            return 1
        fi
        bootstrap_verify_checkout "$destination" "$release"
        return
    fi
    FIREISP_BOOTSTRAP_TEMP="$(mktemp -d "$FIREISP_INSTALLER_ROOT/releases/.download.XXXXXXXX")"
    git init --quiet "$FIREISP_BOOTSTRAP_TEMP"
    git -C "$FIREISP_BOOTSTRAP_TEMP" remote add origin "$FIREISP_REPOSITORY"
    for attempt in 1 2 3; do
        if git -C "$FIREISP_BOOTSTRAP_TEMP" fetch --quiet --depth 1 origin "$release"; then
            fetched=1
            break
        fi
        if ((attempt < 3)); then sleep "$attempt"; fi
    done
    ((fetched)) || { bootstrap_error 'Cannot download the selected release. No installer was executed.'; return 1; }
    git -C "$FIREISP_BOOTSTRAP_TEMP" -c core.hooksPath=/dev/null checkout --quiet --detach FETCH_HEAD
    bootstrap_verify_checkout "$FIREISP_BOOTSTRAP_TEMP" "$release"
    chmod -R a-w -- "$FIREISP_BOOTSTRAP_TEMP"
    mv -- "$FIREISP_BOOTSTRAP_TEMP" "$destination"
    FIREISP_BOOTSTRAP_TEMP=''
}

bootstrap_cleanup() {
    if [[ -n "$FIREISP_BOOTSTRAP_TEMP" ]]; then
        chmod -R u+w -- "$FIREISP_BOOTSTRAP_TEMP" 2>/dev/null || true
        rm -rf -- "$FIREISP_BOOTSTRAP_TEMP"
    fi
}

bootstrap_launch_wizard() {
    export PYTHONDONTWRITEBYTECODE=1
    export GIT_OPTIONAL_LOCKS=0
    exec python3 -B "$FIREISP_INSTALLER_ROOT/releases/$FIREISP_SELECTED_RELEASE/deploy/wizard.py" \
        "${FIREISP_WIZARD_ARGUMENTS[@]}" </dev/tty
}

bootstrap_main() {
    local parsed=0
    bootstrap_parse_arguments "$@" || parsed=$?
    ((parsed != 2)) || return 0
    ((parsed == 0)) || return "$parsed"
    [[ "$EUID" == 0 ]] || { bootstrap_error 'Run this installer as root, for example with sudo bash.'; return 1; }
    # Use the operating system's root tools, not a caller-supplied executable path.
    export PATH='/usr/sbin:/usr/bin:/sbin:/bin'
    if ! (source /etc/os-release; [[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 24.04 ]]); then
        bootstrap_error 'This installer supports Ubuntu 24.04 only.'
        return 1
    fi
    if ! { true </dev/tty; } 2>/dev/null; then
        bootstrap_error 'An interactive terminal is required. Connect with ssh -t, then run the installer again.'
        return 1
    fi
    umask 077
    trap bootstrap_cleanup EXIT
    bootstrap_secure_directory /run 755
    bootstrap_secure_directory /run/fireisp-installer
    command -v flock >/dev/null 2>&1 || { bootstrap_error 'Ubuntu tool flock is missing; install util-linux and retry.'; return 1; }
    [[ ! -L /run/fireisp-installer/bootstrap.lock ]] || { bootstrap_error 'Refusing symbolic-link lock file.'; return 1; }
    exec 9>/run/fireisp-installer/bootstrap.lock
    flock -w 300 9 || { bootstrap_error 'Another installer is downloading a release; try again after it finishes.'; return 1; }
    bootstrap_dependencies
    bootstrap_secure_directory /opt 755
    bootstrap_secure_directory /opt/fireisp 755
    bootstrap_secure_directory "$FIREISP_INSTALLER_ROOT"
    bootstrap_secure_directory "$FIREISP_INSTALLER_ROOT/releases"
    bootstrap_resolve_release
    printf 'Preparing FireISP release %s\n' "$FIREISP_SELECTED_RELEASE"
    bootstrap_checkout "$FIREISP_SELECTED_RELEASE"
    # Do not hold the download lock during the interactive installation.
    flock -u 9
    exec 9>&-
    bootstrap_launch_wizard
}

bootstrap_main "$@"
