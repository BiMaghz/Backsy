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
readonly PYTHON_EXEC="${PYTHON_EXEC:-python3}"
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

# --- Dependency Management ---
check_dependencies() {
    print_header "Checking Dependencies"

    local required=(git curl rsync)
    local missing=()
    local pkg_manager=""
    local cron_pkg=""
    local gpg_pkg=""
    
    # Detect package manager
    if command -v apt-get &>/dev/null; then
        pkg_manager="apt"
        cron_pkg="cron"
        gpg_pkg="gnupg"
    elif command -v dnf &>/dev/null; then
        pkg_manager="dnf"
        cron_pkg="cronie"
        gpg_pkg="gnupg2"
    elif command -v yum &>/dev/null; then
        pkg_manager="yum"
        cron_pkg="cronie"
        gpg_pkg="gnupg2"
    elif command -v pacman &>/dev/null; then
        pkg_manager="pacman"
        cron_pkg="cronie"
        gpg_pkg="gnupg"
    fi

    # Check standard binaries
    for cmd in "${required[@]}"; do
        command -v "$cmd" &>/dev/null || missing+=("$cmd")
    done

    # Check Python
    if ! command -v "$PYTHON_EXEC" &>/dev/null; then
        missing+=("python3")
    fi

    # Check venv support properly
    if ! "$PYTHON_EXEC" -c "import ensurepip" &>/dev/null; then
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

    # Check/Install yq (mikefarah)
    if ! command -v yq &>/dev/null || ! yq --version 2>&1 | grep -qi "mikefarah"; then
        print_warning "Installing yq..."

        if ! is_root; then
            print_error "Root required to install yq."
            exit 1
        fi

        local arch
        case "$(uname -m)" in
            x86_64) arch="amd64" ;;
            aarch64|arm64) arch="arm64" ;;
            *)
                print_error "Unsupported architecture: $(uname -m)"
                exit 1
            ;;
        esac

        curl -fsSL -o /usr/local/bin/yq \
            "https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${arch}" || {
                print_error "Failed to download yq"
                exit 1
            }

        chmod +x /usr/local/bin/yq
    fi

    print_success "All dependencies are installed and ready."
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

    print_header "Target: $name"
    export YQ_NAME="$name"
    yq eval -i ".targets[strenv(YQ_NAME)] = {}" "$CONFIG_FILE"

    # Type
    echo "Select Type:"
    select t in "local" "remote"; do
        [ -n "$t" ] && { yq eval -i ".targets[strenv(YQ_NAME)].type = \"$t\"" "$CONFIG_FILE"; break; }
    done

    # Paths
    echo "
Enter paths to include (comma-separated).

Examples:
    /opt/simple-web, /var/www/html/config
    /etc/ssh/:ssh_configs, /etc/nginx/:nginx_configs

Notes:
    - Use ':' to rename a path inside the backup.
    Format: actual_path:new_name
    - You can enter multiple items separated by commas.
    - Press Enter to skip.
"
    read -r -p "Paths : " p_in
    yq eval -i ".targets[strenv(YQ_NAME)].paths = []" "$CONFIG_FILE"
    if [ -n "$p_in" ]; then
        IFS=',' read -r -a p_arr <<< "$p_in"
        for p in "${p_arr[@]}"; do
            export VAL=$(trim "$p")
            [ -n "$VAL" ] && yq eval -i ".targets[strenv(YQ_NAME)].paths += [strenv(VAL)] | .targets[strenv(YQ_NAME)].paths[-1] style=\"double\"" "$CONFIG_FILE"
        done
    fi

    # Excludes
    echo "
Enter items to exclude from the backup (comma-separated).

Examples:
    mysql, xray, *.dat, logs, errors

Notes:
    - Use '*' for wildcard patterns.
    - Folder or file names will be ignored if matched.
    - Press Enter to skip.
"
    read -r -p "Excludes : " e_in
    yq eval -i ".targets[strenv(YQ_NAME)].exclude = []" "$CONFIG_FILE"
    if [ -n "$e_in" ]; then
        IFS=',' read -r -a e_arr <<< "$e_in"
        for e in "${e_arr[@]}"; do
            export VAL=$(trim "$e")
            [ -n "$VAL" ] && yq eval -i ".targets[strenv(YQ_NAME)].exclude += [strenv(VAL)] | .targets[strenv(YQ_NAME)].exclude[-1] style=\"double\"" "$CONFIG_FILE"
        done
    fi

    # Remote Config
    local t_type
    t_type=$(yq eval ".targets[\"$name\"].type" "$CONFIG_FILE")
    if [ "$t_type" == "remote" ]; then
        read -r -p "Host: " host
        read -r -p "User [root]: " user
        read -r -p "Port [22]: " port
        export H="$host" U="${user:-root}" P="${port:-22}"
        
        yq eval -i ".targets[strenv(YQ_NAME)].host = strenv(H) | .targets[strenv(YQ_NAME)].host style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].user = strenv(U) | .targets[strenv(YQ_NAME)].user style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].port = (strenv(P) | tonumber)" "$CONFIG_FILE"

        echo "Auth Method:"
        select method in "key" "password"; do
            [ -n "$method" ] && { yq eval -i ".targets[strenv(YQ_NAME)].auth.method = \"$method\"" "$CONFIG_FILE"; break; }
        done

        if [ "$method" == "key" ]; then
            read -r -p "Private Key Path: " kp
            export KP="$kp"
            yq eval -i ".targets[strenv(YQ_NAME)].auth.key_path = strenv(KP) | .targets[strenv(YQ_NAME)].auth.key_path style=\"double\"" "$CONFIG_FILE"
        else
            local vname="${name^^}_SSH_PASSWORD"
            vname=${vname//[^A-Z0-9_]/_}
            read -rs -p "SSH Password: " pass; echo
            _add_secret "$vname" "$pass"
            yq eval -i ".targets[strenv(YQ_NAME)].auth.password = \"\${$vname}\"" "$CONFIG_FILE"
        fi
    fi

    # Database Config
    read -r -p "Configure Database? (y/n) [n]: " db_q
    if [[ "${db_q,,}" == "y" ]]; then
        read -r -p "Type (mariadb/mysql/postgresql): " dtype
        read -r -p "Container Name: " dcont
        read -r -p "DB Name: " dname
        read -r -p "DB User: " duser
        
        local vname="${name^^}_DB_PASSWORD"
        vname=${vname//[^A-Z0-9_]/_}
        read -rs -p "DB Password: " dpass; echo
        _add_secret "$vname" "$dpass"

        export DT="$dtype" DC="$dcont" DN="$dname" DU="$duser" DP="\${$vname}"
        yq eval -i ".targets[strenv(YQ_NAME)].database.enable = true" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.type = strenv(DT)" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.container = strenv(DC) | .targets[strenv(YQ_NAME)].database.container style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.name = strenv(DN) | .targets[strenv(YQ_NAME)].database.name style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.user = strenv(DU) | .targets[strenv(YQ_NAME)].database.user style=\"double\"" "$CONFIG_FILE"
        yq eval -i ".targets[strenv(YQ_NAME)].database.password = strenv(DP)" "$CONFIG_FILE"
    else
        yq eval -i "del(.targets[strenv(YQ_NAME)].database)" "$CONFIG_FILE"
    fi
    print_success "Target Configured."
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

    # Cloudflare
    print_info "Cloudflare Worker + KV: A lightweight solution for fast backup transfer."
    read -r -p "Enable Cloudflare? (y/n) [n]: " cf_q
    if [[ "${cf_q,,}" == "y" ]]; then
        read -r -p "Worker URL: " url
        read -rs -p "API Token: (Enter to skip.)" tok; echo
        export URL="$url"
        
        yq eval -i '.services.cloudflare.enable = true' "$CONFIG_FILE"
        yq eval -i '.services.cloudflare.worker_url = strenv(URL) | .services.cloudflare.worker_url style="double"' "$CONFIG_FILE"
        
        if [ -n "$tok" ]; then
            _add_secret "CLOUDFLARE_API_TOKEN" "$tok"
            yq eval -i '.services.cloudflare.api_token = "${CLOUDFLARE_API_TOKEN}"' "$CONFIG_FILE"
        else
            yq eval -i 'del(.services.cloudflare.api_token)' "$CONFIG_FILE"
        fi
    else
        yq eval -i '.services.cloudflare.enable = false' "$CONFIG_FILE"
    fi

    # S3
    read -r -p "Enable S3 Storage (Arvan/R2/MinIO)? (y/n) [n]: " s3_q
    if [[ "${s3_q,,}" == "y" ]]; then
        read -r -p "Endpoint URL (e.g. https://s3.ir-thr-at1.arvanstorage.ir): " s3_url
        read -r -p "Bucket Name: " s3_bucket
        read -r -p "Region Name (optional, press Enter): " s3_region
        
        read -rs -p "Access Key: " s3_access; echo
        read -rs -p "Secret Key: " s3_secret; echo

        read -r -p "Generate S3 Download Links ((24h expiration))? (y/n) [y]: " s3_link_q
        if [[ "${s3_link_q:-y}" == "y" ]]; then
            yq eval -i '.services.s3.generate_link = true' "$CONFIG_FILE"
            print_success "S3 Links Enabled and will be shown in Telegram if Cloudflare is disabled."
        else
            yq eval -i '.services.s3.generate_link = false' "$CONFIG_FILE"
        fi

        _add_secret "S3_ACCESS_KEY" "$s3_access"
        _add_secret "S3_SECRET_KEY" "$s3_secret"
        
        yq eval -i '.services.s3.enable = true' "$CONFIG_FILE"
        yq eval -i ".services.s3.endpoint_url = \"$s3_url\"" "$CONFIG_FILE"
        yq eval -i ".services.s3.bucket_name = \"$s3_bucket\"" "$CONFIG_FILE"
        if [ -n "$s3_region" ]; then
            yq eval -i ".services.s3.region_name = \"$s3_region\"" "$CONFIG_FILE"
        fi
        yq eval -i '.services.s3.access_key = "${S3_ACCESS_KEY}"' "$CONFIG_FILE"
        yq eval -i '.services.s3.secret_key = "${S3_SECRET_KEY}"' "$CONFIG_FILE"
    else
        yq eval -i '.services.s3.enable = false' "$CONFIG_FILE"
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

generate_runner() {
    print_header "Generating Runner"
    mkdir -p "$(dirname "$RUN_SCRIPT")"
    
    cat > "$RUN_SCRIPT" <<EOF
#!/usr/bin/env bash
# Backsy Runner (Auto-Generated)
PROJECT_PATH="$PROJECT_DIR"
VENV_PATH="$VENV_DIR"

# Secrets
EOF

    for key in "${G_SECRET_VARS[@]}"; do
        val="${G_SECRET_VALUES[$key]}"
        # Escape quotes/backslashes
        val="${val//\\/\\\\}"
        val="${val//\"/\\\"}"
        echo "export $key=\"$val\"" >> "$RUN_SCRIPT"
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
    read -r -p "How many targets? [1]: " num
    num=${num:-1}
    for ((i=1; i<=num; i++)); do
        read -r -p "Target #$i Name: " tname
        configure_target "$tname"
    done

    configure_services_and_secrets
    generate_runner
    setup_cron
    print_success "Installation Complete!"
}

manage_backsy() {
    while true; do
        clear
        print_header "Backsy Manager"
        echo "1) Run Manual Backup"
        echo "2) Manage Cron"
        echo "3) View Logs"
        echo "9) Uninstall"
        echo "q) Quit"
        read -r -p "> " opt
        case $opt in
            1) "$RUN_SCRIPT" && print_success "Done" || print_error "Failed"; press_enter ;;
            2) setup_cron; press_enter ;;
            3) tail -n 50 -f "$LOG_FILE";;
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