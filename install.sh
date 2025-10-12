#!/usr/bin/env bash

set -euo pipefail
IFS=$'\n\t'

# =============================================================================
#  Backsy Setup/Management Script
#  Author: BiMaghz (refactored by GPT-5 Thinking mini)
# =============================================================================

# --- Configuration & Constants ---
readonly PROJECT_DIR="/opt/Backsy"
readonly VENV_DIR="$PROJECT_DIR/venv"
readonly CONFIG_FILE="$PROJECT_DIR/config.yml"
readonly RUN_SCRIPT="$PROJECT_DIR/run_backup.sh"
readonly PYTHON_EXEC="${PYTHON_EXEC:-$(command -v python3 || true)}"
readonly REPO_URL="https://github.com/BiMaghz/Backsy.git"
readonly LOG_FILE="/var/log/Backsy.log"

# --- UI Colors ---
readonly C_RESET='\033[0m'
readonly C_RED='\033[0;31m'
readonly C_GREEN='\033[0;32m'
readonly C_YELLOW='\033[0;33m'
readonly C_CYAN='\033[0;36m'
readonly C_BOLD='\033[1m'

# --- Helper Functions for UI ---
print_header()  { printf "\n${C_CYAN}${C_BOLD}=== %s ===${C_RESET}\n" "$1"; }
print_success() { printf "${C_GREEN}[✔] %s${C_RESET}\n" "$1"; }
print_warning() { printf "${C_YELLOW}[!] %s${C_RESET}\n" "$1"; }
print_info()    { printf "${C_CYAN}[i] %s${C_RESET}\n" "$1"; }
print_error()   { printf "${C_RED}[✘] %s${C_RESET}\n" "$1"; }
print_bold()    { printf "${C_BOLD}%s${C_RESET}\n" "$1"; }
press_enter()   { read -r -p "Press Enter to continue..."; }

# --- Utility ---
is_root() { [ "$(id -u)" -eq 0 ]; }

# Trim leading/trailing whitespace
trim() {
    local var="$*"
    var="${var#"${var%%[![:space:]]*}"}"
    var="${var%"${var##*[![:space:]]}"}"
    printf "%s" "$var"
}

# Detect package manager
detect_package_manager() {
    if command -v apt-get &>/dev/null; then echo "apt"; return; fi
    if command -v dnf &>/dev/null; then echo "dnf"; return; fi
    if command -v yum &>/dev/null; then echo "yum"; return; fi
    if command -v pacman &>/dev/null; then echo "pacman"; return; fi
    echo ""
}

# --- Ensure log directory and file exist ---
_prepare_logs() {
    local logdir
    logdir="$(dirname "$LOG_FILE")"
    mkdir -p "$logdir"
    if [ ! -f "$LOG_FILE" ]; then
        : > "$LOG_FILE" 2>/dev/null || true
    fi
    if is_root; then
        chmod 640 "$LOG_FILE" 2>/dev/null || true
    fi
}

# --- YAML helper ---
_set_yaml_value() {
    local yaml_path="$1"
    local value="$2"

    if [ ! -f "$CONFIG_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cat >"$CONFIG_FILE" <<'EOF'
# Backsy Configuration File
targets: {}
services: {}
EOF
        chmod 640 "$CONFIG_FILE" 2>/dev/null || true
    fi

    export YQ_VAL_TO_SET="$value"
    if ! yq eval -i "(${yaml_path}) = (strenv(YQ_VAL_TO_SET) | from_yaml)" "$CONFIG_FILE"; then
        print_error "yq command failed to set value for simple path: $yaml_path"
        return 1
    fi
    return 0
}

# --- Secrets store ---
declare -a G_SECRET_VARS=()
declare -A G_SECRET_VALUES=()

_add_secret_var() {
    local v="$1"
    for existing in "${G_SECRET_VARS[@]:-}"; do
        if [ "$existing" = "$v" ]; then
            return 0
        fi
    done
    G_SECRET_VARS+=("$v")
}

_set_secret_value() {
    local varname="$1"
    local value="$2"
    G_SECRET_VALUES["$varname"]="$value"
}

# --- Configure a backup target interactively ---
configure_target() {
    local target_name="$1"
    if [ -z "$target_name" ]; then
        print_error "Empty target name provided to configure_target"
        return 1
    fi
    print_header "Configuring target: $target_name"

    if [ ! -f "$CONFIG_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cat >"$CONFIG_FILE" <<'EOF'
# Backsy Configuration File
targets: {}
services: {}
EOF
        chmod 640 "$CONFIG_FILE" 2>/dev/null || true
    fi

    export YQ_TARGET_NAME="$target_name"

    yq eval -i ".targets[strenv(YQ_TARGET_NAME)] = {}" "$CONFIG_FILE"

    # --- Type ---
    echo "Select target type:"
    PS3="Enter an option number: "
    select target_type in "local" "remote"; do
        if [ -n "${target_type:-}" ]; then
            export YQ_VALUE="$target_type"
            yq eval -i '.targets[strenv(YQ_TARGET_NAME)].type = strenv(YQ_VALUE)' "$CONFIG_FILE"
            break
        else
            print_warning "Invalid selection; try again."
        fi
    done

    # --- Paths ---
    read -r -p "Enter paths to back up (comma-separated): " paths_input
    yq eval -i '.targets[strenv(YQ_TARGET_NAME)].paths = []' "$CONFIG_FILE"
    if [ -n "${paths_input//[[:space:]]/}" ]; then
        IFS=',' read -r -a paths_arr <<<"$paths_input"
        for p in "${paths_arr[@]}"; do
            p=$(trim "$p")
            [ -z "$p" ] && continue
            export YQ_ITEM="$p"
            yq eval -i '.targets[strenv(YQ_TARGET_NAME)].paths += [strenv(YQ_ITEM)] | .targets[strenv(YQ_TARGET_NAME)].paths[-1] style="double"' "$CONFIG_FILE"
        done
    fi

    # --- Excludes ---
    read -r -p "Enter patterns to exclude (comma-separated): " excl_input
    yq eval -i '.targets[strenv(YQ_TARGET_NAME)].exclude = []' "$CONFIG_FILE"
    if [ -n "${excl_input//[[:space:]]/}" ]; then
        IFS=',' read -r -a excl_arr <<<"$excl_input"
        for e in "${excl_arr[@]}"; do
            e=$(trim "$e")
            [ -z "$e" ] && continue
            export YQ_ITEM="$e"
            yq eval -i '.targets[strenv(YQ_TARGET_NAME)].exclude += [strenv(YQ_ITEM)] | .targets[strenv(YQ_TARGET_NAME)].exclude[-1] style="double"' "$CONFIG_FILE"
        done
    fi

    # --- Remote specific ---
    if [ "$target_type" = "remote" ]; then
        read -r -p "Enter remote host: " host
        read -r -p "Enter SSH user [root]: " user
        user=${user:-root}
        read -r -p "Enter SSH port [22]: " port
        port=${port:-22}

        export YQ_HOST="$host" YQ_USER="$user" YQ_PORT="$port"
        yq eval -i '
            .targets[strenv(YQ_TARGET_NAME)].host = strenv(YQ_HOST) |
            .targets[strenv(YQ_TARGET_NAME)].host style="double" |
            .targets[strenv(YQ_TARGET_NAME)].user = strenv(YQ_USER) |
            .targets[strenv(YQ_TARGET_NAME)].user style="double" |
            .targets[strenv(YQ_TARGET_NAME)].port = (strenv(YQ_PORT) | tonumber)
        ' "$CONFIG_FILE"

        echo "Select authentication method:"
        PS3="Authentication option: "
        select auth_method in "key" "password"; do
            if [ -n "${auth_method:-}" ]; then
                export YQ_AUTH_METHOD="$auth_method"
                yq eval -i '.targets[strenv(YQ_TARGET_NAME)].auth.method = strenv(YQ_AUTH_METHOD)' "$CONFIG_FILE"
                if [ "$auth_method" = "key" ]; then
                    read -r -p "Enter absolute path to private SSH key: " key_path
                    export YQ_KEY_PATH="$key_path"
                    yq eval -i '.targets[strenv(YQ_TARGET_NAME)].auth.key_path = strenv(YQ_KEY_PATH) | .targets[strenv(YQ_TARGET_NAME)].auth.key_path style="double"' "$CONFIG_FILE"
                else
                    local target_name_upper
                    target_name_upper=$(echo "$target_name" | tr '[:lower:]' '[:upper:]' | tr -c '[:alnum:]_' '_')
                    target_name_upper=$(echo "$target_name_upper" | sed 's/_$//')
                    local pass_var="${target_name_upper}_SSH_PASSWORD"
                    export YQ_PASS_VAR="\${${pass_var}}"
                    yq eval -i '.targets[strenv(YQ_TARGET_NAME)].auth.password = strenv(YQ_PASS_VAR)' "$CONFIG_FILE"
                    _add_secret_var "$pass_var"
                    read -r -s -p "Enter SSH password for target '$target_name': " ssh_pass_value
                    echo
                    _set_secret_value "$pass_var" "$ssh_pass_value"
                fi
                break
            else
                print_warning "Invalid selection; try again."
            fi
        done
    fi

    # --- Database optional ---
    read -r -p "Configure a database for this target? (y/n) [n]: " has_db
    has_db=${has_db:-n}
    if [[ "${has_db,,}" == "y" ]]; then
        local db_type
        while true; do
            read -r -p "Enter DB type (mariadb, mysql, postgresql): " db_type
            if [[ "$db_type" == "mariadb" || "$db_type" == "mysql" || "$db_type" == "postgresql" ]]; then
                break
            else
                print_warning "Invalid input. Please enter mariadb, mysql, or postgresql."
            fi
        done
        read -r -p "Enter DB container name (if applicable): " db_container
        read -r -p "Enter DB name: " db_name
        read -r -p "Enter DB user: " db_user

        local target_name_upper
        target_name_upper=$(echo "$target_name" | tr '[:lower:]' '[:upper:]' | tr -c '[:alnum:]_' '_')
        target_name_upper=$(echo "$target_name_upper" | sed 's/_$//')
        local db_pass_var="${target_name_upper}_DB_PASSWORD"
        _add_secret_var "$db_pass_var"
        read -r -s -p "Enter database password for target '$target_name': " db_pass_value
        echo
        _set_secret_value "$db_pass_var" "$db_pass_value"

        export YQ_DB_TYPE="$db_type" YQ_DB_CONTAINER="$db_container" YQ_DB_NAME="$db_name" YQ_DB_USER="$db_user" YQ_DB_PASS_VAR="\${${db_pass_var}}"
        yq eval -i '
            .targets[strenv(YQ_TARGET_NAME)].database.enable = true |
            .targets[strenv(YQ_TARGET_NAME)].database.type = strenv(YQ_DB_TYPE) |
            .targets[strenv(YQ_TARGET_NAME)].database.container = strenv(YQ_DB_CONTAINER) | .targets[strenv(YQ_TARGET_NAME)].database.container style="double" |
            .targets[strenv(YQ_TARGET_NAME)].database.name = strenv(YQ_DB_NAME) | .targets[strenv(YQ_TARGET_NAME)].database.name style="double" |
            .targets[strenv(YQ_TARGET_NAME)].database.user = strenv(YQ_DB_USER) | .targets[strenv(YQ_TARGET_NAME)].database.user style="double" |
            .targets[strenv(YQ_TARGET_NAME)].database.password = strenv(YQ_DB_PASS_VAR)
        ' "$CONFIG_FILE"
    else
        yq eval -i "del(.targets[strenv(YQ_TARGET_NAME)].database)" "$CONFIG_FILE" 2>/dev/null || true
    fi

    print_success "Target '$target_name' configured."
}

# --- Services & Secret configuration ---
configure_services_and_secrets() {
    print_header "Configuring Destination Services"

    # --- Telegram ---
    read -r -p "Enable Telegram notifications? (y/n) [y]: " enable_tg
    enable_tg=${enable_tg:-y}
    if [[ "${enable_tg,,}" == "y" ]]; then
        local tg_token_var="TELEGRAM_TOKEN"
        local tg_chat_var="TELEGRAM_CHAT_ID"
        _set_yaml_value ".services.telegram.enable" true
        _set_yaml_value ".services.telegram.token" "\"\${$tg_token_var}\""
        _set_yaml_value ".services.telegram.chat_id" "\"\${$tg_chat_var}\""
        _add_secret_var "$tg_token_var"
        _add_secret_var "$tg_chat_var"

        read -r -p "Enter your Telegram Chat ID: " tg_chat_value
        read -r -s -p "Enter Telegram Bot Token: " tg_token_value
        echo
        _set_secret_value "$tg_token_var" "$tg_token_value"
        _set_secret_value "$tg_chat_var" "$tg_chat_value"
    else
        _set_yaml_value ".services.telegram.enable" false
    fi

    # --- Cloudflare Worker optional ---
    read -r -p "Enable Cloudflare for backup storage? (y/n) [y]: " enable_cf
    enable_cf=${enable_cf:-y}
    if [[ "${enable_cf,,}" == "y" ]]; then
        local cf_url
        while true; do
            read -r -p "Enter the full Cloudflare Worker URL: " cf_url
            if [[ "$cf_url" =~ ^https?:// ]]; then
                break
            else
                print_warning "Invalid URL. It must start with http:// or https://"
            fi
        done
        _set_yaml_value ".services.cloudflare.enable" true
        _set_yaml_value ".services.cloudflare.worker_url" "\"$cf_url\""

        read -r -p "Does your worker require an API Token for security? (y/n) [y]: " needs_token
        needs_token=${needs_token:-y}
        if [[ "${needs_token,,}" == "y" ]]; then
            local cf_token_var="CLOUDFLARE_API_TOKEN"
            _set_yaml_value ".services.cloudflare.api_token" "\"\${$cf_token_var}\""
            _add_secret_var "$cf_token_var"

            read -r -s -p "Enter Cloudflare API Token: " cf_token_value
            echo
            _set_secret_value "$cf_token_var" "$cf_token_value"
        else
            yq eval -i "del(.services.cloudflare.api_token)" "$CONFIG_FILE" 2>/dev/null || true
        fi
    else
        _set_yaml_value ".services.cloudflare.enable" false
    fi

    print_success "Services configured."
}

# --- Generate run_backup.sh with secret exports ---
generate_run_script() {
    print_header "Generating Execution Script"

    local unique_secrets=()
    local item
    for item in "${G_SECRET_VARS[@]:-}"; do
        if [[ ! " ${unique_secrets[*]} " =~ " ${item} " ]]; then
            unique_secrets+=("$item")
        fi
    done

    mkdir -p "$(dirname "$RUN_SCRIPT")"

    cat >"$RUN_SCRIPT" <<EOF
#!/usr/bin/env bash
# =============================================================================
# BACKUP EXECUTION WRAPPER SCRIPT (auto-generated)
# This script sets up the environment and executes the main Python application.
# =============================================================================

# --- Configuration ---
PROJECT_PATH="$PROJECT_DIR"
VENV_PATH="$VENV_DIR"

# --- Secrets (exports) ---
EOF

    if [ ${#unique_secrets[@]} -gt 0 ]; then
        for secret in "${unique_secrets[@]}"; do
            local value="${G_SECRET_VALUES[$secret]:-}"
            if [ -n "$value" ]; then
                local esc="${value//\\/\\\\}"
                esc="${esc//\"/\\\"}"
                printf "export %s=\"%s\"\n" "$secret" "$esc" >> "$RUN_SCRIPT"
            else
                local placeholder="your_$(echo "$secret" | tr '[:upper:]' '[:lower:]')_value"
                printf "export %s=\"%s\"\n" "$secret" "$placeholder" >> "$RUN_SCRIPT"
            fi
        done
    else
        printf "# No secret environment variables required for this configuration.\n" >> "$RUN_SCRIPT"
    fi

    cat >>"$RUN_SCRIPT" <<'EOF'

# --- Execution Logic ---
cd "$PROJECT_PATH" || { echo "Error: Project directory not found at $PROJECT_PATH"; exit 1; }

if [ ! -f "${VENV_PATH}/bin/activate" ]; then
    echo "Error: Could not find virtual environment activation script." >&2
    exit 1
fi

# Activate the virtual environment
# shellcheck disable=SC1090
source "${VENV_PATH}/bin/activate"

# Execute the main Python application.
# Logging is handled internally by the Python script.
# The exit code of the python script will be the exit code of this script.
python3 -m backuptool.main

EOF

    # --- Set permissions ---
    chmod 700 "$RUN_SCRIPT" 2>/dev/null || true
    print_success "run_backup.sh generated at $RUN_SCRIPT"
    print_warning "If real secrets were exported into $RUN_SCRIPT, ensure its permissions are restricted (e.g. chmod 700)."
}

# --- Setup scheduled backup (cron or systemd) ---
setup_cron() {
    print_header "Setting up Automatic Backup Schedule"

    read -r -p "Do you want to enable automatic backups? (y/n) [y]: " ans
    ans=${ans:-y}
    if [[ "${ans,,}" != "y" ]]; then
        print_info "Skipping automatic backup setup."
        return 0
    fi

    local service_name="backsy-backup"
    local systemd_service_path="/etc/systemd/system/${service_name}.service"
    local systemd_timer_path="/etc/systemd/system/${service_name}.timer"

    if command -v crontab &>/dev/null; then
        local cron_tag="# Backsy Backup Job"
        local cron_cmd="$RUN_SCRIPT >> $LOG_FILE 2>&1"
        read -r -p "Enter cron schedule [0 2 * * *] (2 AM daily): " schedule
        schedule=${schedule:-"0 2 * * *"}

        local cron_entry="$schedule $cron_cmd $cron_tag"

        local current_cron
        current_cron=$(crontab -l 2>/dev/null || true)

        local filtered
        if [ -n "$current_cron" ]; then
            filtered=$(printf "%s\n" "$current_cron" | grep -vF "$RUN_SCRIPT" || true)
        else
            filtered=""
        fi

        {
            printf "%s\n" "$filtered"
            printf "%s\n" "$cron_entry"
        } | crontab -

        print_success "Cron job configured successfully."
        print_info "Schedule: $schedule"
        return 0
    fi

    print_warning "crontab not found — using systemd timer instead."

    if [ "$EUID" -ne 0 ]; then
        print_error "Systemd timers require root privileges. Please re-run this script with sudo."
        return 1
    fi

    read -r -p "Enter backup interval (e.g. 'daily', 'weekly', 'hourly') [daily]: " interval
    interval=${interval:-daily}

    cat >"$systemd_service_path" <<EOF
[Unit]
Description=Backsy Automated Backup

[Service]
Type=oneshot
ExecStart=$RUN_SCRIPT
StandardOutput=append:$LOG_FILE
StandardError=append:$LOG_FILE
EOF

    cat >"$systemd_timer_path" <<EOF
[Unit]
Description=Run Backsy Backup $interval

[Timer]
OnCalendar=$interval
Persistent=true

[Install]
WantedBy=timers.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now "${service_name}.timer"

    print_success "Systemd timer created and started."
}

# --- Uninstall ---
uninstall_script() {
    print_header "Backsy Uninstallation"
    print_warning "This will remove the application folder and any scheduled jobs (cron or systemd timer)."
    read -r -p "Are you sure you want to proceed? (y/n): " confirm
    if [[ "${confirm,,}" != "y" ]]; then
        echo "Uninstallation aborted."
        return 0
    fi

    if command -v crontab &>/dev/null; then
        crontab -l 2>/dev/null | grep -vF "$RUN_SCRIPT" | crontab - 2>/dev/null || true
        print_success "Cron job removed (if present)."
    fi

    local service_name="backsy-backup"
    if systemctl list-timers --all 2>/dev/null | grep -q "$service_name"; then
        if [ "$EUID" -ne 0 ]; then
            print_warning "Systemd timer found, but needs root to remove. Please re-run with sudo if needed."
        else
            systemctl stop "${service_name}.timer" 2>/dev/null || true
            systemctl disable "${service_name}.timer" 2>/dev/null || true
            rm -f "/etc/systemd/system/${service_name}.service" "/etc/systemd/system/${service_name}.timer"
            systemctl daemon-reload
            print_success "Systemd timer removed."
        fi
    fi

    if [ -d "$PROJECT_DIR" ]; then
        rm -rf "$PROJECT_DIR"
        print_success "Project directory $PROJECT_DIR removed."
    else
        print_info "Project directory not found; skipping removal."
    fi

    print_success "Uninstallation complete."
}

# --- Helpers for management menu ---
_get_targets() {
    if [ -f "$CONFIG_FILE" ]; then
        yq eval '.targets // {} | keys | .[]' "$CONFIG_FILE" 2>/dev/null || true
    fi
}

is_valid_var_name() {
    if [[ "$1" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
        return 0
    else
        print_error "Invalid variable name: '$1'. Must start with a letter or underscore, followed by letters, numbers, or underscores."
        return 1
    fi
}

# --- Management menu ---
management_menu() {
    while true; do
        clear
        printf "\n${C_CYAN}=================================================${C_RESET}\n"
        printf "                    ${C_YELLOW}Backsy - Management Menu${C_RESET}\n"
        printf "${C_CYAN}=================================================${C_RESET}\n"
        echo " 1) Run a Manual Backup"
        echo " 2) Manage Cron Job"
        echo " 3) View Live Logs"
        echo " 9) Uninstall Backsy"
        echo " q) Quit"
        echo "-------------------------------------------------"
        read -r -p "Enter your choice: " choice

        case $choice in
        1)
            if [ -x "$RUN_SCRIPT" ]; then
                print_info "Running manual backup now..."

                if ! bash "$RUN_SCRIPT"; then
                    print_warning "Manual backup returned a non-zero exit code."
                    print_info "Check $LOG_FILE for details."
                else
                    print_success "Manual backup finished successfully."
                fi
            else
                print_warning "Run script not found or not executable: $RUN_SCRIPT"
            fi
            press_enter
            ;;
        2)
            setup_cron
            press_enter
            ;;
        3)
            _prepare_logs
            if [ -f "$LOG_FILE" ]; then
                print_info "Tailing log file. Press Ctrl-C to stop."
                trap 'printf "\nStopping log tail\n"; trap - INT; break' INT
                ( tail -n 200 -f "$LOG_FILE" ) || true
                trap - INT
            else
                print_warning "Log file not found: $LOG_FILE"
            fi
            press_enter
            ;;
        9)
            uninstall_script
            exit 0
            ;;
        q|Q)
            echo "Goodbye!"
            exit 0
            ;;
        *)
            print_warning "Invalid choice, please try again."
            press_enter
            ;;
        esac
    done
}

# --- Installation process ---
install_script() {
    print_header "Backsy Installation"

    if [ -d "$PROJECT_DIR" ]; then
        print_error "Project directory $PROJECT_DIR already exists. Aborting installation."
        exit 1
    fi

    check_dependencies
    _prepare_logs

    print_header "Cloning repository"
    if ! git clone "$REPO_URL" "$PROJECT_DIR"; then
        print_error "Failed to clone repository. Check URL and network connection."
        exit 1
    fi
    cd "$PROJECT_DIR" || exit 1

    print_header "Setting up Python Environment"
    "$PYTHON_EXEC" -m venv "$VENV_DIR"
    if [ -x "${VENV_DIR}/bin/pip" ]; then
        "${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
        if [ -f "requirements.txt" ]; then
            "${VENV_DIR}/bin/pip" install --no-cache-dir -r requirements.txt || print_warning "pip install had issues; check manually."
        fi
    else
        print_warning "venv pip not found; skipping pip install. Check venv at $VENV_DIR"
    fi

    for f in config.yml.example setup.sh; do
        [ -f "$f" ] && rm -f "$f"
    done

    # create_initial_config

    print_header "Initial Configuration Wizard"
    read -r -p "Enter the number of backup targets to configure [1]: " num_targets
    num_targets=${num_targets:-1}
    if ! [[ "$num_targets" =~ ^[0-9]+$ ]]; then
        print_warning "Invalid number, assuming 0 targets."
        num_targets=0
    fi

    for ((i=1; i<=num_targets; i++)); do
        read -r -p "Enter a name for target #$i: " target_name
        target_name=$(trim "$target_name")
        if [ -n "$target_name" ]; then
            configure_target "$target_name"
        else
            print_warning "Empty name; skipping target #$i."
        fi
    done

    configure_services_and_secrets || { print_warning "Service configuration returned non-zero; continuing." ; }

    generate_run_script

    setup_cron

    print_success "Installation complete!"
    print_info "If secrets were saved, they may be stored in: $RUN_SCRIPT"
    print_warning "Restrict access to the run script: sudo chmod 700 $RUN_SCRIPT"
    echo
    echo "You can manage your installation by running this script again."
}

check_dependencies() {
    print_header "Checking dependencies..."
    local pm
    pm=$(detect_package_manager)

    local required_pkgs=("git" "python3" "pip" "curl" "rsync")
    local missing_pkgs=()
    for pkg in "${required_pkgs[@]}"; do
        if ! command -v "$pkg" &>/dev/null; then
            if [[ "$pkg" == "pip" ]] && command -v "pip3" &>/dev/null; then
                continue
            fi
            missing_pkgs+=("$pkg")
        fi
    done

    if ! python3 -c "import ensurepip" &>/dev/null; then
        print_warning "Python's 'venv' module is missing required components (ensurepip)."
        if [[ ! " ${missing_pkgs[*]} " =~ " python3-venv " ]]; then
             missing_pkgs+=("python3-venv")
        fi
    fi

    if [ ${#missing_pkgs[@]} -gt 0 ]; then
        print_warning "Missing required system packages: ${missing_pkgs[*]}"
        if [ -z "$pm" ]; then
            print_error "Could not detect package manager. Please install missing packages manually and re-run."
            exit 1
        fi

        local install_cmd
        local venv_pkg="python3-venv"

        case "$pm" in
            apt)
                local py_ver
                py_ver=$(python3 --version 2>/dev/null | cut -d ' ' -f 2 | cut -d '.' -f 1,2)
                if [[ -n "$py_ver" ]]; then
                    venv_pkg="python${py_ver}-venv"
                fi
                install_cmd="sudo apt-get update && sudo apt-get install -y git python3 python3-pip ${venv_pkg} curl rsync"
                ;;
            dnf) install_cmd="sudo dnf install -y git python3 python3-pip curl rsync" ;;
            yum) install_cmd="sudo yum install -y git python3 python3-pip curl rsync" ;;
            pacman) install_cmd="sudo pacman -S --noconfirm git python python-pip curl rsync" ;;
            *)
                print_error "Unsupported package manager '$pm'. Please install manually: ${missing_pkgs[*]}"
                exit 1
                ;;
        esac

        print_info "Attempting to install missing packages with command: $install_cmd"
        read -r -p "Do you want to proceed with the installation? (y/n) [y]: " confirm
        confirm=${confirm:-y}
        if [[ "${confirm,,}" != "y" ]]; then
            print_error "Installation aborted by user."
            exit 1
        fi

        if ! eval "$install_cmd"; then
            print_error "Failed to install dependencies. Please install them manually and re-run the script."
            exit 1
        fi
        print_success "System packages installed."
    fi

    if command -v yq &>/dev/null && yq --version 2>&1 | grep -q "mikefarah" && yq --version 2>&1 | grep -q "version v4"; then
        print_success "yq (mikefarah v4+) is already installed."
        return 0
    fi

    print_warning "yq (mikefarah v4+) not found or is wrong version. Attempting to install..."

    if ! is_root; then
        print_error "Root privileges are required to install yq to /usr/local/bin. Please run script with sudo."
        exit 1
    fi

    local arch yq_arch
    arch=$(uname -m)
    case "$arch" in
        x86_64) yq_arch="amd64" ;;
        aarch64 | arm64) yq_arch="arm64" ;;
        *)
            print_error "Unsupported architecture '$arch' for automatic yq installation."
            print_info "Please install yq (mikefarah v4+) manually from https://github.com/mikefarah/yq/releases"
            exit 1
            ;;
    esac

    local yq_url="https://github.com/mikefarah/yq/releases/latest/download/yq_linux_${yq_arch}"
    local yq_dest="/usr/local/bin/yq"

    print_info "Downloading yq for $arch from $yq_url..."
    if ! curl -Lso "$yq_dest" "$yq_url"; then
        print_error "Failed to download yq. Check network connection or install manually."
        [ -f "$yq_dest" ] && rm -f "$yq_dest"
        exit 1
    fi

    print_info "Setting permissions for yq..."
    if ! chmod +x "$yq_dest"; then
        print_error "Failed to make yq executable. Check permissions for $yq_dest."
        exit 1
    fi

    if command -v yq &>/dev/null && yq --version &>/dev/null; then
        print_success "yq successfully installed to $yq_dest."
        yq --version
    else
        print_error "yq installation failed verification. Please check manually."
        exit 1
    fi
}

# --- Main entrypoint ---
main() {
    if [ -d "$PROJECT_DIR" ]; then
        cd "$PROJECT_DIR" || exit 1
        management_menu
    else
        if ! is_root; then
            print_error "Installation must be run as root or with sudo."
            exit 1
        fi
        install_script
    fi
}

main "$@"