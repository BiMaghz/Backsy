#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'

# =============================================================================
#  Backsy Setup/Management Script (refactored by GPT-5)
# =============================================================================

# --- Configuration ---
readonly PROJECT_DIR="/opt/Backsy"
readonly VENV_DIR="$PROJECT_DIR/venv"
readonly CONFIG_FILE="$PROJECT_DIR/config.yml"
readonly RUN_SCRIPT="$PROJECT_DIR/run_backup.sh"
PYTHON_EXEC="${PYTHON_EXEC:-}"   # chosen by select_python (must be >= 3.10)
readonly KNOWN_HOSTS="$PROJECT_DIR/known_hosts"

# yq is pinned and verified (SHA-256 from the project's published checksums file).
# To upgrade: change the version and both hashes together.
readonly YQ_VERSION="v4.54.1"
readonly YQ_SHA256_AMD64="8e34fc298390875de416e6a4afcb8cabeceb25d9aa8506c1a2f9353cf702ea5f"
readonly YQ_SHA256_ARM64="189088da0c6429ec5178dfaab1a114805f6cab0b61b165ab236efedf1d57a71b"
readonly REPO_URL="https://github.com/BiMaghz/Backsy.git"
readonly LOG_FILE="/var/log/Backsy.log"

# --- Colors ---
readonly C_RESET='\033[0m'
readonly C_RED='\033[0;31m'
readonly C_GREEN='\033[0;32m'
readonly C_YELLOW='\033[0;33m'
readonly C_CYAN='\033[0;36m'
readonly C_BOLD='\033[1m'

# --- UI Helpers ---
print_header()  { printf "\n${C_CYAN}${C_BOLD}=== %s ===${C_RESET}\n" "$1"; }
print_success() { printf "${C_GREEN}[✔] %s${C_RESET}\n" "$1"; }
print_warning() { printf "${C_YELLOW}[!] %s${C_RESET}\n" "$1"; }
print_info()    { printf "${C_CYAN}[i] %s${C_RESET}\n" "$1"; }
print_error()   { printf "${C_RED}[✘] %s${C_RESET}\n" "$1"; }
press_enter()   { read -r -p "Press Enter to continue..."; }

# --- Utils ---
is_root() { [ "$(id -u)" -eq 0 ]; }

trim() {
    local var="$*"
    var="${var#"${var%%[![:space:]]*}"}"
    var="${var%"${var##*[![:space:]]}"}"
    printf "%s" "$var"
}

_prepare_logs() {
    mkdir -p "$(dirname "$LOG_FILE")"
    [ ! -f "$LOG_FILE" ] && : > "$LOG_FILE"
    is_root && chmod 640 "$LOG_FILE" 2>/dev/null || true
}

_resolve_path() {
    local path="$1"
    if [[ "$path" == "~"* ]]; then
        local user_home
        user_home=$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)
        path="${path/#\~/$user_home}"
    fi
    realpath -q "$path" || echo "$path"
}

# --- Secrets Management ---
declare -a G_SECRET_VARS=()
declare -A G_SECRET_VALUES=()

_add_secret() {
    local var="$1" val="$2"
    if [[ ! " ${G_SECRET_VARS[*]} " =~ " ${var} " ]]; then
        G_SECRET_VARS+=("$var")
    fi
    G_SECRET_VALUES["$var"]="$val"
}

# --- Python / name / host-key helpers ---
_python_ok() {
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' &>/dev/null
}

select_python() {
    local cand
    for cand in "$PYTHON_EXEC" python3.13 python3.12 python3.11 python3.10 python3; do
        [ -n "$cand" ] || continue
        if command -v "$cand" &>/dev/null && _python_ok "$cand"; then
            PYTHON_EXEC="$cand"
            return 0
        fi
    done
    return 1
}

_valid_target_name() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]
}

# Shows the server's host key fingerprints and, on confirmation, pins them in $KNOWN_HOSTS.
trust_host_key() {
    local host="$1" port="$2" scan tmp entry
    scan=$(ssh-keyscan -T 10 -p "$port" "$host" 2>/dev/null || true)
    if [ -z "$scan" ]; then
        print_error "Could not fetch host keys from $host:$port"
        return 1
    fi

    tmp=$(mktemp)
    printf '%s\n' "$scan" > "$tmp"
    print_info "Host key fingerprints offered by $host:$port:"
    ssh-keygen -lf "$tmp" | sed 's/^/    /'
    read -r -p "Trust these keys? Compare them with the server's own fingerprints first. (y/n) [n]: " ans
    if [[ "${ans,,}" != "y" ]]; then
        rm -f "$tmp"
        return 1
    fi

    entry="$host"
    [ "$port" != "22" ] && entry="[$host]:$port"
    touch "$KNOWN_HOSTS"; chmod 600 "$KNOWN_HOSTS"
    ssh-keygen -R "$entry" -f "$KNOWN_HOSTS" &>/dev/null || true   # replace any older key for this host
    rm -f "$KNOWN_HOSTS.old"
    cat "$tmp" >> "$KNOWN_HOSTS"
    rm -f "$tmp"
    print_success "Host key saved to $KNOWN_HOSTS"
}

# Manager action: (re)trust the host keys of every remote target in config.yml.
trust_all_remote_hosts() {
    print_header "Trust SSH Host Keys"
    local rows name host port
    rows=$(yq eval '.targets | to_entries | .[] | select(.value.type == "remote") | .key + " " + .value.host + " " + ((.value.port // 22) | tostring)' "$CONFIG_FILE")
    if [ -z "$rows" ]; then
        print_info "No remote targets configured."
        return 0
    fi
    # Rows come in on fd 3 so that stdin stays free for trust_host_key's confirmation prompt.
    while IFS=' ' read -r -u 3 name host port; do   # IFS is newline/tab script-wide
        [ -n "$name" ] || continue
        print_info "Target: $name"
        trust_host_key "$host" "$port" || print_warning "Not trusted: $host:$port"
    done 3<<< "$rows"
}

# --- Compression (gzip / zstd) ---
compression_algorithm() {
    [ -f "$CONFIG_FILE" ] || { echo "gzip"; return 0; }
    yq eval '.compression.algorithm // "gzip"' "$CONFIG_FILE" 2>/dev/null || echo "gzip"
}

ensure_zstd() {
    command -v zstd &>/dev/null && return 0
    print_warning "zstd not found. Installing..."
    if ! is_root; then
        print_error "Root is required to install zstd (e.g. 'apt install zstd')."
        return 1
    fi
    if command -v apt-get &>/dev/null; then
        apt-get update -qq && apt-get install -y zstd
    elif command -v dnf &>/dev/null; then
        dnf install -y zstd
    elif command -v yum &>/dev/null; then
        yum install -y zstd
    elif command -v pacman &>/dev/null; then
        pacman -Sy --noconfirm zstd
    else
        print_error "Could not install zstd automatically. Please install the 'zstd' package manually."
        return 1
    fi
    command -v zstd &>/dev/null
}

# Remote servers run tar and the compressor themselves: warn when zstd is selected but missing there.
# Usage: check_remote_zstd HOST COMMAND... (the command runs the given string on the remote host)
check_remote_zstd() {
    local host="$1"; shift
    [ "$(compression_algorithm)" = "zstd" ] || return 0
    if "$@" "command -v zstd" &>/dev/null; then
        print_success "zstd is installed on $host."
    else
        print_warning "zstd is not installed on $host. Backups of this target will fall back to gzip until you install it (e.g. 'apt install zstd')."
    fi
}

configure_compression() {
    _init_config
    print_header "Compression"
    echo "1) gzip - default (uses pigz when installed)"
    echo "2) zstd - much faster; must be installed on this server and on every remote server"
    local choice algo="gzip"
    read -r -p "Choose [1]: " choice
    if [[ "${choice:-1}" == "2" ]]; then
        if ensure_zstd; then
            algo="zstd"
        else
            print_warning "Keeping gzip."
        fi
    fi
    export YQ_ALGO="$algo"
    yq eval -i '.compression.algorithm = strenv(YQ_ALGO)' "$CONFIG_FILE"
    print_success "Compression set to: $algo"
}

# Cloudflare Worker/KV support was removed; a leftover enabled section makes every run fail.
warn_removed_cloudflare() {
    [ -f "$CONFIG_FILE" ] || return 0
    if [ "$(yq eval '.services.cloudflare.enable // false' "$CONFIG_FILE" 2>/dev/null || echo false)" = "true" ]; then
        print_warning "Cloudflare support has been removed, but 'services.cloudflare' is still enabled in config.yml."
        print_warning "Backups will fail until you delete that section (and CLOUDFLARE_API_TOKEN from run_backup.sh)."
    fi
}

# The config may have been switched to zstd by hand: make sure this server can honour it.
ensure_compression_tools() {
    if [ "$(compression_algorithm)" = "zstd" ]; then
        ensure_zstd || print_warning "zstd is selected but not installed here; backups will fall back to gzip."
    fi
}

# --- Dependency Management ---
check_dependencies() {
    print_header "Checking Dependencies"

    local required=(git curl rsync sshpass ssh-keyscan)
    local ssh_pkg=""
    local missing=()
    local pkg_manager=""    
    local cron_pkg=""
    local gpg_pkg=""
    
    # Detect package manager
    if command -v apt-get &>/dev/null; then
        pkg_manager="apt"
        cron_pkg="cron"
        gpg_pkg="gnupg"
        ssh_pkg="openssh-client"
    elif command -v dnf &>/dev/null; then
        pkg_manager="dnf"
        cron_pkg="cronie"
        gpg_pkg="gnupg2"
        ssh_pkg="openssh-clients"
    elif command -v yum &>/dev/null; then
        pkg_manager="yum"
        cron_pkg="cronie"
        gpg_pkg="gnupg2"
        ssh_pkg="openssh-clients"
    elif command -v pacman &>/dev/null; then
        pkg_manager="pacman"
        cron_pkg="cronie"
        gpg_pkg="gnupg"
        ssh_pkg="openssh"
    fi

    # Check standard binaries
    for cmd in "${required[@]}"; do
        if ! command -v "$cmd" &>/dev/null; then
            if [ "$cmd" == "ssh-keyscan" ]; then
                [ -n "$ssh_pkg" ] && missing+=("$ssh_pkg")
            else
                missing+=("$cmd")
            fi
        fi
    done

    # Check Python (Backsy needs 3.10+)
    if ! select_python; then
        if command -v python3 &>/dev/null; then
            print_error "Backsy needs Python 3.10 or newer, but found: $(python3 -V 2>&1)"
            print_info "Install a newer Python (e.g. python3.11), or run with PYTHON_EXEC=/path/to/python3.x"
            exit 1
        fi
        missing+=("python3")
    fi

    # Check venv support properly
    if [ -z "$PYTHON_EXEC" ] || ! "$PYTHON_EXEC" -c "import ensurepip" &>/dev/null; then
         missing+=("python3-venv")
    fi

    # Check Cron
    if ! command -v crontab &>/dev/null && [[ -n "$cron_pkg" ]]; then
        missing+=("$cron_pkg")
    fi

    # Check GPG
    if ! command -v gpg &>/dev/null && [[ -n "$gpg_pkg" ]]; then
        missing+=("$gpg_pkg")
    fi

    # Install missing packages
    if [[ ${#missing[@]} -gt 0 ]]; then
        print_warning "Missing packages: ${missing[*]}"

        if ! is_root; then
            print_error "Run with sudo to auto-install dependencies."
            exit 1
        fi

        case "$pkg_manager" in
            apt)
                apt-get update
                apt-get install -y "${missing[@]}"
            ;;
            dnf)
                dnf install -y "${missing[@]}"
            ;;
            yum)
                yum install -y "${missing[@]}"
            ;;
            pacman)
                pacman -Sy --noconfirm "${missing[@]}"
            ;;
            *)
                print_error "Unsupported package manager. Install manually: ${missing[*]}"
                exit 1
            ;;
        esac
    fi

    # Packages may just have brought in Python: re-check the version.
    if ! select_python; then
        print_error "No Python 3.10+ interpreter available. Install one (e.g. python3.11) or set PYTHON_EXEC."
        exit 1
    fi

    # Check/Install yq (mikefarah)
    if ! command -v yq &>/dev/null || ! yq --version 2>&1 | grep -qi "mikefarah"; then
        print_warning "Installing yq..."

        if ! is_root; then
            print_error "Root required to install yq."
            exit 1
        fi

        local arch expected
        case "$(uname -m)" in
            x86_64)        arch="amd64"; expected="$YQ_SHA256_AMD64" ;;
            aarch64|arm64) arch="arm64"; expected="$YQ_SHA256_ARM64" ;;
            *)
                print_error "Unsupported architecture: $(uname -m)"
                exit 1
            ;;
        esac

        local tmp_yq
        tmp_yq=$(mktemp)
        curl -fsSL -o "$tmp_yq" \
            "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${arch}" || {
                rm -f "$tmp_yq"
                print_error "Failed to download yq"
                exit 1
            }

        if ! echo "${expected}  ${tmp_yq}" | sha256sum -c --status -; then
            rm -f "$tmp_yq"
            print_error "yq checksum mismatch - refusing to install it."
            exit 1
        fi
        install -m 755 "$tmp_yq" /usr/local/bin/yq
        rm -f "$tmp_yq"
    fi

    print_success "All dependencies are installed and ready."
}

ensure_db_client() {
    local type=$1
    
    if [ "$target_type" == "local" ]; then
        if [[ "$type" == "mariadb" ]] || [[ "$type" == "mysql" ]]; then
            if ! command -v mariadb-dump &>/dev/null && ! command -v mysqldump &>/dev/null; then
                print_warning "MariaDB/MySQL client tools not found. Installing..."
                if command -v apt-get &>/dev/null; then
                    apt-get update -qq && apt-get install -y mariadb-client
                elif command -v dnf &>/dev/null; then
                    dnf install -y mariadb
                elif command -v yum &>/dev/null; then
                    yum install -y mariadb
                elif command -v pacman &>/dev/null; then
                    pacman -S --noconfirm mariadb
                else
                    print_error "Could not install database client automatically. Please install 'mariadb-client' manually."
                fi
            fi
        elif [[ "$type" == "postgresql" ]]; then
            if ! command -v pg_dump &>/dev/null; then
                print_warning "PostgreSQL client tools not found. Installing..."
                if command -v apt-get &>/dev/null; then
                    apt-get update -qq && apt-get install -y postgresql-client
                elif command -v dnf &>/dev/null; then
                    dnf install -y postgresql
                elif command -v pacman &>/dev/null; then
                    pacman -S --noconfirm postgresql-libs
                else
                    print_error "Could not install database client automatically. Please install 'postgresql-client' manually."
                fi
            fi
        fi
    else
        print_info "Note: For remote native backups, ensure database clients (mysqldump/pg_dump) are installed on the remote server."
    fi
}

# --- Configuration Logic ---
_init_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        echo "targets: {}" > "$CONFIG_FILE"
        echo "services: {}" >> "$CONFIG_FILE"
        chmod 640 "$CONFIG_FILE"
    fi
}

configure_target() {
    local name="${1:-$(hostname)}"
    _init_config

    print_header "Configuring Target: $name"
    export YQ_NAME="$name"
    yq eval -i ".targets[strenv(YQ_NAME)] = {}" "$CONFIG_FILE"

    echo "Select Type:"
    PS3="Your choice > "
    local target_type="local"
    select t in "local" "remote"; do
        [ -n "$t" ] && { target_type="$t"; yq eval -i ".targets[strenv(YQ_NAME)].type = \"$t\"" "$CONFIG_FILE"; break; }
    done

    local -a REMOTE_CMD_ARR=()

    if [ "$target_type" == "remote" ]; then
        while true; do
            print_info "Configuring Remote SSH Connection..."
            read -r -p "Host: " host
            read -r -p "User [root]: " user
            read -r -p "Port [22]: " port
            
            export H="$host" U="${user:-root}" P="${port:-22}"

            if ! trust_host_key "$H" "$P"; then
                print_warning "Host key not trusted. Please re-enter the connection details."
                continue
            fi
            
            yq eval -i ".targets[strenv(YQ_NAME)].host = strenv(H) | .targets[strenv(YQ_NAME)].host style=\"double\"" "$CONFIG_FILE"
            yq eval -i ".targets[strenv(YQ_NAME)].user = strenv(U) | .targets[strenv(YQ_NAME)].user style=\"double\"" "$CONFIG_FILE"
            yq eval -i ".targets[strenv(YQ_NAME)].port = (strenv(P) | tonumber)" "$CONFIG_FILE"

            echo "Auth Method:"
            local method="key"
            select m in "key" "password"; do
                [ -n "$m" ] && { method="$m"; yq eval -i ".targets[strenv(YQ_NAME)].auth.method = \"$m\"" "$CONFIG_FILE"; break; }
            done

            local kp=""
            local pass_val=""

            local -a ssh_base_opts=(
                -o StrictHostKeyChecking=yes
                -o UserKnownHostsFile="$KNOWN_HOSTS"
                -o ConnectTimeout=10 
                -o LogLevel=ERROR
                -p "$P"
            )

            REMOTE_CMD_ARR=()

            if [ "$method" == "key" ]; then
                read -e -p "Private Key Path: " raw_kp
                kp=$(_resolve_path "$raw_kp")
                
                if [ ! -f "$kp" ]; then
                    print_error "Key file not found at: $kp"
                    continue
                fi

                export KP="$kp"
                yq eval -i ".targets[strenv(YQ_NAME)].auth.key_path = strenv(KP) | .targets[strenv(YQ_NAME)].auth.key_path style=\"double\"" "$CONFIG_FILE"
                
                REMOTE_CMD_ARR=(ssh "${ssh_base_opts[@]}" -o BatchMode=yes -o IdentitiesOnly=yes -i "$kp" "$U@$H")

            else
                local vname="${name^^}_SSH_PASSWORD"
                vname=${vname//[^A-Z0-9_]/_}
                read -rs -p "SSH Password: " pass_val; echo
                _add_secret "$vname" "$pass_val"
                yq eval -i ".targets[strenv(YQ_NAME)].auth.password = \"\${$vname}\"" "$CONFIG_FILE"

                if ! command -v sshpass &>/dev/null; then
                    print_warning "'sshpass' is missing. Cannot verify connection automatically."
                    break
                fi
                
                export SSHPASS="$pass_val"   # sshpass -e reads it from the environment, so it never appears in `ps`
                REMOTE_CMD_ARR=(sshpass -e ssh "${ssh_base_opts[@]}" "$U@$H")
            fi

            # --- Test Connection ---
            print_info "Testing connection to $H..."

            local test_output
            if test_output=$("${REMOTE_CMD_ARR[@]}" "echo ConnectionSuccess" 2>&1); then
                if [[ "$test_output" == *"ConnectionSuccess"* ]]; then
                    print_success "Connection Established Successfully!"
                    check_remote_zstd "$H" "${REMOTE_CMD_ARR[@]}"
                    break
                fi
            fi

            print_error "Connection Failed!"
            print_warning "Error Details:"
            echo -e "${C_RED}$test_output${C_RESET}"
            echo "---------------------------------------------------"
        done
    fi

    # Paths
    print_info "Enter paths to backup one by one."
    print_info "Press 'Enter' on an empty line to finish."
    print_info "Tip: Use 'path:folder' to store a path under folder/ inside the backup."
    
    if [ "$target_type" == "remote" ]; then
        print_info "Tip: Type 'explore' to open a temporary shell on the server to find paths."
    fi

    yq eval -i ".targets[strenv(YQ_NAME)].paths = []" "$CONFIG_FILE"
    
    while true; do
        read -e -p "Add Path > " p_in
        if [ -z "$p_in" ]; then break; fi
        
        # --- Explore Mode ---
        if [[ "$p_in" == "explore" || "$p_in" == "sh" ]] && [ "$target_type" == "remote" ]; then
             print_warning "Opening temporary shell on remote server..."
             print_info "Navigate to find your path, copy it, then type 'exit' to return here."
             
             if [ "$method" == "key" ]; then
                 ssh "${ssh_base_opts[@]}" -t -o BatchMode=yes -o IdentitiesOnly=yes -i "$kp" "$U@$H" bash || true
             else
                 sshpass -e ssh "${ssh_base_opts[@]}" -t "$U@$H" bash || true
             fi
             
             echo
             print_success "Returned to setup."
             continue
        fi

        export VAL=$(trim "$p_in")
        yq eval -i ".targets[strenv(YQ_NAME)].paths += [strenv(VAL)] | .targets[strenv(YQ_NAME)].paths[-1] style=\"double\"" "$CONFIG_FILE"
    done
    
    # Excludes
    print_info "Enter items to exclude (comma-separated)."
    print_info "Example: *.log, temp/, /var/cache"

    read -r -p "Excludes > " e_in
    yq eval -i ".targets[strenv(YQ_NAME)].exclude = []" "$CONFIG_FILE"
    if [ -n "$e_in" ]; then
        IFS=',' read -r -a e_arr <<< "$e_in"
        for e in "${e_arr[@]}"; do
            export VAL=$(trim "$e")
            [ -n "$VAL" ] && yq eval -i ".targets[strenv(YQ_NAME)].exclude += [strenv(VAL)] | .targets[strenv(YQ_NAME)].exclude[-1] style=\"double\"" "$CONFIG_FILE"
        done
    fi

    # Database Config
    read -r -p "Configure Database? (y/n) [n]: " db_q
    if [[ "${db_q,,}" == "y" ]]; then
        while true; do
            read -r -p "Type (mariadb/mysql/postgresql): " dtype
            dtype=${dtype,,}
            if [[ "$dtype" == "mariadb" || "$dtype" == "mysql" || "$dtype" == "postgresql" ]]; then
                break
            else
                print_error "Invalid type. Please enter mariadb, mysql, or postgresql."
            fi
        done

        local dcont=""
        local dhost=""
        local dport=""
        local containers=()
        
        if [ "$target_type" == "remote" ]; then
            if [ ${#REMOTE_CMD_ARR[@]} -gt 0 ]; then
                print_info "Fetching remote containers..."
                local docker_out
                if docker_out=$("${REMOTE_CMD_ARR[@]}" "docker ps --format '{{.Names}}'" 2>/dev/null); then
                    if [ -n "$docker_out" ]; then
                        mapfile -t containers <<< "$docker_out"
                    else
                        print_warning "No running containers found on remote."
                    fi
                fi
            fi
        elif command -v docker &>/dev/null; then
             mapfile -t containers < <(docker ps --format "{{.Names}}")
        fi

        if [ ${#containers[@]} -gt 0 ]; then
            echo "Select Database Source:"
            PS3="Select Container (or Manual/Native): "
            select opt in "${containers[@]}" "Manual Input" "Native (No Container)"; do
                if [[ "$opt" == "Native (No Container)" ]]; then
                    dcont=""
                    break
                elif [[ "$opt" == "Manual Input" ]]; then
                    read -r -p "Enter Container Name: " dcont
                    break
                elif [[ -n "$opt" ]]; then
                    dcont="$opt"
                    break
                else
                    echo "Invalid selection."
                fi
            done
        else
            read -r -p "Container Name (Leave empty for Native/Localhost): " dcont
        fi

        if [ -z "$dcont" ]; then
            ensure_db_client "$dtype"
            
            read -r -p "DB Host [127.0.0.1]: " dhost
            dhost=${dhost:-127.0.0.1}
            
            local def_port="3306"
            if [[ "$dtype" == "postgresql" ]]; then def_port="5432"; fi
            
            read -r -p "DB Port [$def_port]: " dport
            dport=${dport:-$def_port}
        fi

        read -r -p "DB Name: " dname
        read -r -p "DB User: " duser
        local vname="${name^^}_DB_PASSWORD"
        vname=${vname//[^A-Z0-9_]/_}
        read -rs -p "DB Password: " dpass; echo
        _add_secret "$vname" "$dpass"
        
        export DT="$dtype" DC="$dcont" DN="$dname" DU="$duser" DP="\${$vname}" DH="$dhost" DPRT="$dport"
        
        yq eval -i ".targets[strenv(YQ_NAME)].database.enable = true" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.type = strenv(DT)" "$CONFIG_FILE"
        if [ -n "$DC" ]; then
            yq eval -i ".targets[strenv(YQ_NAME)].database.container = strenv(DC) | .targets[strenv(YQ_NAME)].database.container style=\"double\"" "$CONFIG_FILE"
            yq eval -i "del(.targets[strenv(YQ_NAME)].database.host)" "$CONFIG_FILE"
            yq eval -i "del(.targets[strenv(YQ_NAME)].database.port)" "$CONFIG_FILE"
        else
            yq eval -i "del(.targets[strenv(YQ_NAME)].database.container)" "$CONFIG_FILE"
            yq eval -i ".targets[strenv(YQ_NAME)].database.host = strenv(DH)" "$CONFIG_FILE"
            yq eval -i ".targets[strenv(YQ_NAME)].database.port = env(DPRT)" "$CONFIG_FILE" 
        fi
        yq eval -i ".targets[strenv(YQ_NAME)].database.name = strenv(DN) | .targets[strenv(YQ_NAME)].database.name style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.user = strenv(DU) | .targets[strenv(YQ_NAME)].database.user style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.password = strenv(DP)" "$CONFIG_FILE"
    else
        yq eval -i "del(.targets[strenv(YQ_NAME)].database)" "$CONFIG_FILE"
    fi
    print_success "Target Configured."
}

# Env-var prefix for an S3 destination's secrets: "offsite-2" -> S3_OFFSITE_2
_s3_env_prefix() {
    printf 'S3_%s' "$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]' | tr -c 'A-Z0-9' '_')"
}

# True if an existing S3 destination would use the same secret names (e.g. "a-b" and "a_b").
_s3_prefix_in_use() {
    local wanted="$1" existing
    while IFS= read -r existing; do
        [ -n "$existing" ] || continue
        if [ "$(_s3_env_prefix "$existing")" = "$wanted" ]; then
            return 0
        fi
    done < <(yq eval '.services.s3 | keys | .[]' "$CONFIG_FILE" 2>/dev/null || true)
    return 1
}

# Older configs keep one S3 block directly under `s3:`. Turn it into the named destination "default"
# so further destinations can be added next to it (mixing both formats is rejected by Backsy).
migrate_legacy_s3() {
    [ -f "$CONFIG_FILE" ] || return 0
    if [ "$(yq eval '.services.s3 | (has("enable") or has("bucket_name"))' "$CONFIG_FILE" 2>/dev/null || echo false)" = "true" ]; then
        yq eval -i '.services.s3 = {"default": .services.s3}' "$CONFIG_FILE"
        print_info "Converted the existing S3 settings to a named destination: 'default'."
    fi
}

# Adds one or more named S3 destinations to config.yml (used by the wizard and the manager menu).
configure_s3_destinations() {
    _init_config
    migrate_legacy_s3

    local s3_name s3_url s3_bucket s3_region s3_prefix s3_style s3_access s3_secret s3_link_q more envp ans links
    # Telegram shows one link (the first destination with links enabled wins), so offer it once.
    local default_link="y"
    if [ "$(yq eval '[.services.s3[] | select(.generate_link == true)] | length' "$CONFIG_FILE" 2>/dev/null || echo 0)" != "0" ]; then
        default_link="n"
    fi

    while true; do
        while true; do
            read -r -p "Destination name (e.g. primary, offsite): " s3_name
            if ! _valid_target_name "$s3_name"; then
                print_error "Use 1-64 letters, digits, '.', '_' or '-' (starting with a letter or digit)."
                continue
            fi
            if [ "$(S3_NAME="$s3_name" yq eval '.services.s3 | has(strenv(S3_NAME))' "$CONFIG_FILE" 2>/dev/null || echo false)" = "true" ]; then
                print_error "A destination named '$s3_name' already exists."
                continue
            fi
            envp=$(_s3_env_prefix "$s3_name")
            if _s3_prefix_in_use "$envp"; then
                print_error "That name would reuse the secret names ${envp}_*. Please pick a different one."
                continue
            fi
            break
        done

        while true; do
            read -r -p "Endpoint URL (e.g. https://s3.ir-thr-at1.arvanstorage.ir): " s3_url
            if [[ "$s3_url" =~ ^https?:// ]]; then
                break
            fi
            print_warning "Invalid URL. Please start with http:// or https://"
        done

        read -r -p "Bucket Name: " s3_bucket
        read -r -p "Region Name (optional, press Enter): " s3_region
        read -r -p "Key prefix inside the bucket, e.g. backsy/ (optional, press Enter): " s3_prefix
        echo "Addressing style: 1) auto (default)   2) path (MinIO and some providers)   3) virtual"
        read -r -p "Choose [1]: " ans
        case "${ans:-1}" in
            2) s3_style="path" ;;
            3) s3_style="virtual" ;;
            *) s3_style="auto" ;;
        esac

        read -rs -p "Access Key: " s3_access; echo
        read -rs -p "Secret Key: " s3_secret; echo

        links=false
        read -r -p "Generate S3 Download Links (24h expiration)? (y/n) [$default_link]: " s3_link_q
        ans="${s3_link_q:-$default_link}"
        if [[ "${ans,,}" == "y" ]]; then
            links=true
            default_link="n"
            print_success "S3 Links Enabled and will be shown in Telegram."
        fi

        _add_secret "${envp}_ACCESS_KEY" "$s3_access"
        _add_secret "${envp}_SECRET_KEY" "$s3_secret"

        export S3_NAME="$s3_name" S3_URL="$s3_url" S3_BUCKET="$s3_bucket" S3_REGION="$s3_region" \
               S3_PREFIX="$s3_prefix" S3_STYLE="$s3_style" S3_LINKS="$links" \
               S3_AK_REF="\${${envp}_ACCESS_KEY}" S3_SK_REF="\${${envp}_SECRET_KEY}"
        yq eval -i '
            .services.s3[strenv(S3_NAME)].enable = true |
            .services.s3[strenv(S3_NAME)].endpoint_url = strenv(S3_URL) |
            .services.s3[strenv(S3_NAME)].endpoint_url style="double" |
            .services.s3[strenv(S3_NAME)].bucket_name = strenv(S3_BUCKET) |
            .services.s3[strenv(S3_NAME)].addressing_style = strenv(S3_STYLE) |
            .services.s3[strenv(S3_NAME)].generate_link = (strenv(S3_LINKS) == "true") |
            .services.s3[strenv(S3_NAME)].access_key = strenv(S3_AK_REF) |
            .services.s3[strenv(S3_NAME)].access_key style="double" |
            .services.s3[strenv(S3_NAME)].secret_key = strenv(S3_SK_REF) |
            .services.s3[strenv(S3_NAME)].secret_key style="double"
        ' "$CONFIG_FILE"
        if [ -n "$s3_region" ]; then
            yq eval -i '.services.s3[strenv(S3_NAME)].region_name = strenv(S3_REGION)' "$CONFIG_FILE"
        fi
        if [ -n "$s3_prefix" ]; then
            yq eval -i '.services.s3[strenv(S3_NAME)].prefix = strenv(S3_PREFIX)' "$CONFIG_FILE"
        fi
        unset S3_NAME S3_URL S3_BUCKET S3_REGION S3_PREFIX S3_STYLE S3_LINKS S3_AK_REF S3_SK_REF

        print_success "S3 destination '$s3_name' added."
        read -r -p "Add another S3 destination? (y/n) [n]: " more
        if [[ "${more,,}" != "y" ]]; then
            break
        fi
    done
}

# Adds the secrets collected by _add_secret to an existing runner (manager mode: the runner already exists).
append_runner_secrets() {
    if [ ! -f "$RUN_SCRIPT" ] || ! grep -q '^cd "\$PROJECT_PATH"' "$RUN_SCRIPT"; then
        print_error "Could not update $RUN_SCRIPT automatically. Add these lines to it, before the 'cd' line:"
        local k
        for k in "${G_SECRET_VARS[@]}"; do echo "export $k='<value>'"; done
        return 1
    fi
    local tmp line key val safe_val inserted=false
    tmp=$(mktemp)
    chmod 600 "$tmp"
    while IFS= read -r line; do
        if [ "$inserted" = false ] && [[ "$line" == 'cd "$PROJECT_PATH"'* ]]; then
            for key in "${G_SECRET_VARS[@]}"; do
                val="${G_SECRET_VALUES[$key]}"
                safe_val="${val//\'/\'\\\'\'}"
                echo "export $key='$safe_val'" >> "$tmp"
            done
            echo >> "$tmp"
            inserted=true
        fi
        printf '%s\n' "$line" >> "$tmp"
    done < "$RUN_SCRIPT"
    install -m 700 "$tmp" "$RUN_SCRIPT"
    rm -f "$tmp"
}

# Manager action: add S3 destinations to a running installation.
add_s3_from_manager() {
    print_header "Add S3 Destination"
    G_SECRET_VARS=()
    G_SECRET_VALUES=()
    configure_s3_destinations
    if [ "${#G_SECRET_VARS[@]}" -gt 0 ]; then
        append_runner_secrets && print_success "Credentials saved to $RUN_SCRIPT"
    fi
}

configure_services_and_secrets() {
    print_header "Services Configuration"
    _init_config

    # Telegram
    read -r -p "Enable Telegram? (y/n) [n]: " tg_q
    if [[ "${tg_q,,}" == "y" ]]; then
        print_info "Format for Chat ID: 'YourID' OR 'YourID/TopicID' (e.g., -100123456/5)"
        read -r -p "Chat ID: " raw_cid
        read -rs -p "Bot Token: " tok; echo
        read -r -p "Send actual backup files to Telegram? (If 'n', only text notification with links will be sent) (y/n) [y]: " send_file_q
        if [[ "${send_file_q:-y}" == "n" ]]; then
            yq eval -i '.services.telegram.send_file = false' "$CONFIG_FILE"
            print_info "Telegram set to 'Notification Only' mode."
        else
            yq eval -i '.services.telegram.send_file = true' "$CONFIG_FILE"
        fi
        
        local real_cid="$raw_cid"
        local topic_id=""
        
        if [[ "$raw_cid" == *"/"* ]]; then
            real_cid="${raw_cid%%/*}"
            topic_id="${raw_cid##*/}"
        fi

        _add_secret "TELEGRAM_TOKEN" "$tok"
        _add_secret "TELEGRAM_CHAT_ID" "$real_cid"
        
        yq eval -i '.services.telegram.enable = true' "$CONFIG_FILE"
        yq eval -i '.services.telegram.token = "${TELEGRAM_TOKEN}"' "$CONFIG_FILE"
        yq eval -i '.services.telegram.chat_id = "${TELEGRAM_CHAT_ID}"' "$CONFIG_FILE"
        
        if [ -n "$topic_id" ]; then
            _add_secret "TELEGRAM_TOPIC_ID" "$topic_id"
            yq eval -i '.services.telegram.topic_id = "${TELEGRAM_TOPIC_ID}"' "$CONFIG_FILE"
        else
            yq eval -i 'del(.services.telegram.topic_id)' "$CONFIG_FILE"
        fi
    else
        yq eval -i '.services.telegram.enable = false' "$CONFIG_FILE"
    fi

    # S3 (one or more named destinations)
    read -r -p "Enable S3 Storage (Arvan/R2/MinIO/AWS)? (y/n) [n]: " s3_q
    if [[ "${s3_q,,}" == "y" ]]; then
        configure_s3_destinations
    fi

    # Encryption Setup
    print_header "Security & Encryption"
    read -r -p "Encrypt backups with GPG? (Recommended) (y/n) [y]: " do_enc
    
    if [[ "${do_enc:-y}" == "y" ]]; then
        while true; do
            read -rs -p "Enter Encryption Password: " enc_pass; echo
            read -rs -p "Confirm Password: " enc_pass2; echo

            if [[ -n "$enc_pass" && "$enc_pass" == "$enc_pass2" ]]; then
                _add_secret "BACKUP_ENCRYPTION_PASSWORD" "$enc_pass"
                print_success "Encryption enabled."
                break
            else
                print_error "Passwords do not match or are empty. Try again."
            fi
            done
        fi
}

configure_monitoring() {
    print_header "Monitoring Configuration"
    print_info "Integrate with Healthchecks.io, Uptime Kuma, or Better Stack."

    while true; do
        read -r -p "Enter Healthcheck URL (Enter to skip): " hc_url

        if [[ -z "$hc_url" ]]; then
            print_warning "Skipped Healthcheck configuration."
            break
        fi

        if [[ "$hc_url" =~ ^https?:// ]]; then
            yq eval -i ".monitoring.healthcheck_url = \"$hc_url\" | .monitoring.healthcheck_url style=\"double\"" "$CONFIG_FILE"
            print_success "Healthcheck configured."
            break
        else
            print_warning "Invalid URL format. Please enter a valid URL (starting with http:// or https://)"
        fi
    done
}

generate_runner() {
    print_header "Generating Runner"
    mkdir -p "$(dirname "$RUN_SCRIPT")"
    # Create it 0700 *before* any secret is written (no window where it is world-readable).
    install -m 700 /dev/null "$RUN_SCRIPT"

    cat > "$RUN_SCRIPT" <<EOF
#!/usr/bin/env bash
# Backsy Runner (Auto-Generated)
PROJECT_PATH="$PROJECT_DIR"
VENV_PATH="$VENV_DIR"

# Secrets
EOF

    for key in "${G_SECRET_VARS[@]}"; do
        val="${G_SECRET_VALUES[$key]}"        
        safe_val="${val//\'/\'\\\'\'}"
        echo "export $key='$safe_val'" >> "$RUN_SCRIPT"
    done

    cat >> "$RUN_SCRIPT" <<'EOF'

cd "$PROJECT_PATH" || exit 1
[ -f "${VENV_PATH}/bin/activate" ] && source "${VENV_PATH}/bin/activate"
exec python3 -m backuptool.main
EOF
    chmod 700 "$RUN_SCRIPT"
    print_success "Runner generated: $RUN_SCRIPT"
}

setup_cron() {
    print_header "Cron Job"
    local cron_tag="# Backsy Backup Job"
    local cron_cmd="$RUN_SCRIPT >> $LOG_FILE 2>&1"
    
    read -r -p "Setup Cron? (y/n) [y]: " do_cron
    if [[ "${do_cron:-y}" == "y" ]]; then
        read -r -p "Schedule [0 2 * * *]: " sch
        sch=${sch:-"0 2 * * *"}
        (crontab -l 2>/dev/null | grep -vF "$cron_tag"; echo "$sch $cron_cmd $cron_tag") | crontab -
        print_success "Cron set: $sch"
    else
        (crontab -l 2>/dev/null | grep -vF "$cron_tag") | crontab - 2>/dev/null || true
        print_info "Cron removed/skipped."
    fi
}

# --- Actions ---
install_backsy() {
    [ -d "$PROJECT_DIR" ] && { print_error "Already installed at $PROJECT_DIR"; exit 1; }
    
    check_dependencies
    _prepare_logs

    print_info "Cloning..."
    git clone "$REPO_URL" "$PROJECT_DIR" || exit 1
    cd "$PROJECT_DIR"

    print_info "Setup venv..."
    "$PYTHON_EXEC" -m venv "$VENV_DIR"
    "${VENV_DIR}/bin/pip" install --no-cache-dir -r requirements.txt

    print_header "Wizard"
    configure_compression
    read -r -p "How many targets? [1]: " num
    num=${num:-1}
    for ((i=1; i<=num; i++)); do
        while true; do
            read -r -p "Target #$i Name: " tname
            _valid_target_name "$tname" && break
            print_error "Use 1-64 letters, digits, '.', '_' or '-' (starting with a letter or digit)."
        done
        configure_target "$tname"
    done

    configure_services_and_secrets
    configure_monitoring
    generate_runner
    setup_cron
    print_success "Installation Complete!"
}

manage_backsy() {
    warn_removed_cloudflare
    ensure_compression_tools
    while true; do
        clear
        print_header "Backsy Manager"
        echo "1) Run Manual Backup"
        echo "2) Manage Cron"
        echo "3) View Logs"
        echo "4) Trust SSH Host Keys"
        echo "5) Change Compression (gzip/zstd)"
        echo "6) Add S3 Destination"
        echo "9) Uninstall"
        echo "q) Quit"
        read -r -p "> " opt
        case $opt in
            1) "$RUN_SCRIPT" && print_success "Done" || print_error "Failed"; press_enter ;;
            2) setup_cron; press_enter ;;
            3) tail -n 50 -f "$LOG_FILE";;
            4) trust_all_remote_hosts; press_enter ;;
            5) configure_compression; press_enter ;;
            6) add_s3_from_manager; press_enter ;;
            9) uninstall_backsy; exit 0 ;;
            q) exit 0 ;;
            *) ;;
        esac
    done
}

uninstall_backsy() {
    print_warning "Uninstalling Backsy..."
    read -r -p "Confirm? (y/n): " c
    [[ "${c,,}" != "y" ]] && return

    (crontab -l 2>/dev/null | grep -vF "# Backsy Backup Job") | crontab - 2>/dev/null || true
    rm -rf "$PROJECT_DIR"
    print_success "Removed."
}

# --- Main ---
if ! is_root; then
    print_error "Must run as root/sudo."
    exit 1
fi

if [ -d "$PROJECT_DIR" ]; then
    cd "$PROJECT_DIR" || exit 1
    manage_backsy
else
    install_backsy
fi