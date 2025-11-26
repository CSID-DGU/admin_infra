# ~/.bashrc: executed by bash(1) for non-login shells.

# If not running interactively, don't do anything
case $- in
    *i*) ;;
      *) return;;
esac

# Create log directory early
mkdir -p $HOME/.kube_logs

# Basic bash settings
export HISTCONTROL=ignoreboth
export HISTSIZE=1000
export HISTFILESIZE=2000
shopt -s histappend
shopt -s checkwinsize

# Colored prompt
export PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '

# Enable color support
if [ -x /usr/bin/dircolors ]; then
    test -r ~/.dircolors && eval "$(dircolors -b ~/.dircolors)" || eval "$(dircolors -b)"
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
    alias fgrep='fgrep --color=auto'
    alias egrep='egrep --color=auto'
fi

# Common aliases
alias ll='ls -alF'
alias la='ls -A'
alias l='ls -CF'

# -----------------------------------------------------------------------------
# Run-once guard for logout to prevent duplicate executions from alias/trap/monitor
# -----------------------------------------------------------------------------
run_logout_once() {
    # Determine a stable session identifier
    # Priority: SESSION_ID (exported by ssh_session_monitor) > passed arg > ORIGINAL_PID > current $$
    local sid="${SESSION_ID:-${1:-${ORIGINAL_PID:-$$}}}"

    # Create an atomic lock file under /tmp, unique per session
    local lock="/tmp/.logout_once_${sid}"

    # Atomic creation using noclobber so only the first caller succeeds
    ( set -o noclobber; echo "$$ $(date +%s)" > "$lock" ) 2>/dev/null || {
        # Already executed once for this session; just return
        echo "$(kst_date): logout already executed once for SID=${sid} (lock=${lock})" >> $HOME/.kube_logs/monitor.log
        return 0
    }

    # Actually run the logout script exactly once
    bash $HOME/.bash_logout
}

# -----------------------------------------------------------------------------
# Logout function - can be configured for blocking or non-blocking mode
# -----------------------------------------------------------------------------
safe_logout() {
    local original_pid=${1:-$$}
    local blocking_mode=${LOGOUT_BLOCKING_MODE:-false}

    export ORIGINAL_PID="$original_pid"

    if [ "$blocking_mode" = "true" ]; then
        # Blocking mode: wait for script completion, respect exit codes
        run_logout_once "$original_pid"
        return $?
    else
        # Non-blocking mode: run in background
        run_logout_once "$original_pid" >/dev/null 2>&1 &
        return 0
    fi
}

# -----------------------------------------------------------------------------
# Set up traps with run-once guard via safe_logout
# -----------------------------------------------------------------------------
trap 'echo "$(kst_date): EXIT trap triggered for PID $$" >> $HOME/.kube_logs/monitor.log; ORIGINAL_PID=$$ safe_logout $$' EXIT

# Test trap that won't block
test_trap() {
    echo "$(kst_date): Test trap executed for PID $$" >> /tmp/trap_test.log
    safe_logout $$
}
trap 'test_trap' USR1

# Create an alias for exit that won't block
alias exit='safe_logout $$; builtin exit'

# Create a more user-friendly logout command
alias bye='safe_logout $$; builtin exit'
alias quit='safe_logout $$; builtin exit'

# -----------------------------------------------------------------------------
# SSH session monitoring approach with better logging and unique session tracking
# -----------------------------------------------------------------------------

# Korean time function - simplified and more reliable
kst_date() {
    # Manual UTC + 9 hours (most reliable in containers)
    local utc_epoch=$(date -u +%s)
    local kst_epoch=$((utc_epoch + 32400))  # +9 hours in seconds

    # Use specific format if no args provided
    if [ $# -eq 0 ]; then
        date -d "@$kst_epoch" '+%Y-%m-%d %H:%M:%S KST' 2>/dev/null || date '+%Y-%m-%d %H:%M:%S'
    else
        date -d "@$kst_epoch" "$@" 2>/dev/null || date "$@"
    fi
}

ssh_session_monitor() {
    # Capture the main bash PID before entering subshell
    local main_bash_pid=$$
    local session_id="${main_bash_pid}_$(date +%s)_${RANDOM}"
    local session_file="/tmp/ssh_session_$session_id"

    # Export SESSION_ID so run_once guard can identify the session
    export SESSION_ID="$session_id"

    # Create persistent monitor log directory
    mkdir -p "$HOME/.kube_logs"
    # Use fixed filename that continues across pod restarts
    local monitor_log="$HOME/.kube_logs/monitor.log"

    # Prevent multiple monitors for the same PID
    if [ -f "/tmp/monitor_active_${main_bash_pid}" ]; then
        echo "$(kst_date): Monitor already active for PID ${main_bash_pid}, skipping" >> "$monitor_log"
        return
    fi

    # Mark this PID as having active monitor
    echo "$session_id" > "/tmp/monitor_active_${main_bash_pid}"

    # Create session marker with more details
    echo "$(kst_date): Session started - PID: ${main_bash_pid}, PPID: $(ps -o ppid= -p ${main_bash_pid} | tr -d ' '), Session ID: $session_id" >> "$monitor_log"
    echo "$(kst_date): Environment - PWD: $PWD, USER: $USER, SSH_CLIENT: ${SSH_CLIENT:-none}" >> "$monitor_log"
    echo "$session_id" > "$session_file"

    # Monitor SSH connection in a more robust way
    {
        local loop_count=0
        local initial_ppid=$(ps -o ppid= -p ${main_bash_pid} 2>/dev/null | tr -d ' ')
        echo "$(kst_date): Starting session monitor for PID ${main_bash_pid} (initial PPID: $initial_ppid)" >> "$monitor_log"

        while true; do
            sleep 5
            loop_count=$((loop_count + 1))

            # Log monitoring activity every 6 loops (30 seconds)
            if [ $((loop_count % 6)) -eq 0 ]; then
                echo "$(kst_date): Monitor active - Loop $loop_count, PID ${main_bash_pid} still running" >> "$monitor_log"
            fi

            # Check if parent bash process is still alive - more thorough check
            if ! kill -0 ${main_bash_pid} 2>/dev/null || ! ps -p ${main_bash_pid} >/dev/null 2>&1; then
                echo "$(kst_date): Bash process ${main_bash_pid} terminated - triggering logout script" >> "$monitor_log"
                safe_logout ${main_bash_pid}
                rm -f "$session_file" "/tmp/monitor_active_${main_bash_pid}"
                echo "$(kst_date): Session monitor for PID ${main_bash_pid} completed" >> "$monitor_log"
                break
            fi

            # Alternative approach: Monitor parent process changes instead of terminal
            current_ppid=$(ps -o ppid= -p ${main_bash_pid} 2>/dev/null | tr -d ' ')
            process_exists=$(ps -p ${main_bash_pid} >/dev/null 2>&1 && echo "yes" || echo "no")
            is_bash=$(ps -p ${main_bash_pid} -o args= 2>/dev/null | grep -q "bash" && echo "yes" || echo "no")

            # Log detailed status every 5 loops for debugging
            if [ $((loop_count % 5)) -eq 0 ]; then
                echo "$(kst_date): Debug - PID ${main_bash_pid} PPID: $current_ppid, Process: $process_exists, IsBash: $is_bash" >> "$monitor_log"
            fi

            # Check if parent process changed (SSH disconnection detection)
            # Only trigger if PPID changed from initial value AND became orphaned
            if [ "$process_exists" = "yes" ] && [ "$is_bash" = "yes" ] && [ "$initial_ppid" != "0" ] && [ "$initial_ppid" != "1" ]; then
                # Only check if current PPID is different and orphaned
                if ([ "$current_ppid" = "1" ] || [ "$current_ppid" = "0" ]) && [ "$current_ppid" != "$initial_ppid" ]; then
                    echo "$(kst_date): Parent process died (PPID: $initial_ppid -> $current_ppid) for PID ${main_bash_pid} - SSH likely disconnected" >> "$monitor_log"

                    # Wait and verify
                    sleep 3
                    recheck_ppid=$(ps -o ppid= -p ${main_bash_pid} 2>/dev/null | tr -d ' ')

                    echo "$(kst_date): PPID recheck: $recheck_ppid" >> "$monitor_log"

                    # If still orphaned and bash exists, trigger logout
                    if ([ "$recheck_ppid" = "1" ] || [ "$recheck_ppid" = "0" ]) && kill -0 ${main_bash_pid} 2>/dev/null; then
                        echo "$(kst_date): CONFIRMED SSH disconnection for PID ${main_bash_pid} (orphaned process, PPID=$recheck_ppid) - triggering logout script" >> "$monitor_log"
                        safe_logout ${main_bash_pid}
                        rm -f "$session_file" "/tmp/monitor_active_${main_bash_pid}"
                        echo "$(kst_date): Session monitor for PID ${main_bash_pid} completed" >> "$monitor_log"
                        break
                    else
                        echo "$(kst_date): False alarm for PID ${main_bash_pid} - Parent process restored (PPID=$recheck_ppid)" >> "$monitor_log"
                    fi
                fi
            fi
        done
    } &

    # Disown the background job to prevent job control messages
    disown $!

    # Store the monitor info
    echo "Monitor started for session $session_id at $(kst_date)" > "$HOME/.kube_logs/monitor_${session_id}.info"
}

# Start monitoring when bash starts (only for interactive shells)
if [[ $- == *i* ]]; then
    ssh_session_monitor
fi

# Also monitor through PROMPT_COMMAND for interactive sessions
check_session() {
    # Simple check for session activity
    local current_ppid=$(ps -o ppid= -p $$ 2>/dev/null | tr -d ' ')
    if [ -n "$current_ppid" ] && [ "$current_ppid" != "1" ]; then
        # Session is active - just update timestamp
        echo "$(date +%s)" > "/tmp/session_active_$$"
    fi
}

PROMPT_COMMAND="check_session"

# Useful monitoring commands
alias monitor_status='echo "=== Persistent monitor files ==="; ls -la $HOME/.kube_logs/monitor_* 2>/dev/null; echo "=== Temp session files ==="; ls -la /tmp/monitor_active_* /tmp/ssh_session_* 2>/dev/null || echo "No temp files found"'
alias cleanup_old_monitors='echo "Persistent logs:"; ls -la $HOME/.kube_logs/monitor_* 2>/dev/null; echo "Temp files:"; find /tmp -name "monitor_*" -o -name "ssh_session_*" -o -name "session_active_*" | xargs ls -la 2>/dev/null; echo "Use: rm $HOME/.kube_logs/monitor_* /tmp/monitor_* /tmp/ssh_session_* /tmp/session_active_* to clean up"'
alias monitor_log='cat $HOME/.kube_logs/monitor.log 2>/dev/null || echo "No monitor log found"'
alias logout_log='ls -la $HOME/.kube_logs/ 2>/dev/null && cat $HOME/.kube_logs/logout.log 2>/dev/null || echo "No logout logs found"'
alias session_info='echo "Current PID: $$, PPID: $(ps -o ppid= -p $$), Terminal: $([ -t 0 ] && echo "Connected" || echo "Disconnected")"'
alias timezone_info='echo "System time: $(date)"; echo "UTC time: $(date -u)"; echo "KST attempt: $(kst_date)"; ls -la /usr/share/zoneinfo/Asia/Seoul 2>/dev/null || echo "Seoul timezone file not found"'

# Version tracking
BASHRC_VERSION="v0.10"

cd ~

# Welcome message with current session info
echo "Welcome to ContainerSSH! Logout logging is enabled. ($BASHRC_VERSION)"
echo "Current session: PID=$$, Terminal=$([ -t 0 ] && echo 'Connected' || echo 'Disconnected')"
echo "Available logout commands: exit, bye, quit"
echo "Session monitoring commands:"
echo "  monitor_status - Show monitor file status"
echo "  monitor_log    - Show current session monitor log"
echo "  logout_log     - Show logout logs"
echo "  session_info   - Show current session info"
echo "  timezone_info  - Debug timezone settings"
echo "  kill -USR1 $$  - Test logout script manually"
