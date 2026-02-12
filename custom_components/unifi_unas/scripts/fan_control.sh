#!/bin/bash

set -euo pipefail

# =============================================================================
# Constants
# =============================================================================

readonly HDD_DEVICES=(sda sdb sdc sdd sde sdf sdg sdh)
readonly TREND_FALLING_FAST=-1.5
readonly TREND_FALLING=0
readonly TREND_STABLE=0.3
readonly PI_RESTORE_WINDOW=300
readonly STALE_TEMP_THRESHOLD=30
readonly INTEGRAL_REDUCE_FACTOR=0.25

# Debug toggles
# SYMMETRIC_DECAY: when true, decay rate also uses response_speed multiplier (0.5x/1x/2x)
#                  when false, decay always uses 1x rate regardless of response_speed
SYMMETRIC_DECAY=true

# =============================================================================
# Configuration (replaced at deploy time)
# =============================================================================

MQTT_HOST="REPLACE_ME"
MQTT_USER="REPLACE_ME"
MQTT_PASS="REPLACE_ME"
MQTT_ROOT="REPLACE_ME"
MQTT_SYSTEM="${MQTT_ROOT}/system"
MQTT_CONTROL="${MQTT_ROOT}/control"
MQTT_FAN="${MQTT_CONTROL}/fan"

STATE_FILE="/tmp/fan_control_state"
LAST_PWM_FILE="/tmp/fan_control_last_pwm"
SHARED_TEMP_FILE="/tmp/unas_hdd_temp"
MONITOR_INTERVAL_FILE="/tmp/unas_monitor_interval"
LOG_FILE="/var/log/fan_control.log"
LOG_MAX_SIZE=26214400  # 25MB (~48 hours)
LOG_BACKUPS=3  # keep .1, .2, .3 (~1 week total)

# =============================================================================
# State variables
# =============================================================================

FAN_MODE="unas_managed"
MIN_TEMP=40
MAX_TEMP=50
MIN_FAN=64
MAX_FAN=255
TARGET_TEMP=42
TEMP_METRIC="max"
RESPONSE_SPEED="balanced"

PI_INTEGRAL=0
PI_LAST_PWM=0
PI_LAST_TIME=0
PI_RESULT=0
PI_TREND_MULT="1.0"
PREV_FAN_MODE=""
PREV_TARGET_TEMP=""
SAVED_PI_INTEGRAL=""
SAVED_PI_TIME=0

PI_KP=10
PI_KI=0.05
PI_MAX_RATE=5

TEMP_HISTORY=""
TEMP_HISTORY_SIZE=6
LAST_TEMP_FILE_MTIME=0

# =============================================================================
# Logging functions
# =============================================================================

rotate_log_if_needed() {
    if [ -f "$LOG_FILE" ]; then
        local size
        size=$(stat -c %s "$LOG_FILE" 2>/dev/null || echo 0)
        if [ "$size" -gt "$LOG_MAX_SIZE" ]; then
            # shift older backups
            for i in $(seq $((LOG_BACKUPS - 1)) -1 1); do
                [ -f "${LOG_FILE}.$i" ] && mv "${LOG_FILE}.$i" "${LOG_FILE}.$((i + 1))"
            done
            mv "$LOG_FILE" "${LOG_FILE}.1"
        fi
    fi
}

log() {
    local timestamp msg
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    msg="[$timestamp] $*"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE"
}

# =============================================================================
# Utility functions
# =============================================================================

pwm_to_percent() {
    echo "$(($1 * 100 / 255))%"
}

is_integer() {
    [[ "$1" =~ ^[0-9]+$ ]]
}

temp_to_int() {
    echo "${1%.*}"
}

format_data_source() {
    [ "$1" = "fallback" ] && echo "direct poll" || echo "${1}s old"
}

escape_sed_replacement() {
    printf '%s' "$1" | sed -e 's/[\\/&]/\\&/g'
}

float_compare() {
    awk -v a="$1" -v op="$2" -v b="$3" 'BEGIN {
        if (op == ">") exit !(a > b)
        if (op == "<") exit !(a < b)
        if (op == ">=") exit !(a >= b)
        if (op == "<=") exit !(a <= b)
        if (op == "==") exit !(a == b)
        exit 1
    }'
}

set_pwm() {
    echo "$1" > /sys/class/hwmon/hwmon0/pwm1
    echo "$1" > /sys/class/hwmon/hwmon0/pwm2
}

# =============================================================================
# MQTT functions
# =============================================================================

update_state_from_mqtt() {
    local topic=$1 payload=$2
    local var_name

    case "${topic##*/}" in
        mode)
            var_name="FAN_MODE"
            ;;
        min_temp)
            is_integer "$payload" || return
            var_name="MIN_TEMP"
            ;;
        max_temp)
            is_integer "$payload" || return
            var_name="MAX_TEMP"
            ;;
        min_fan)
            is_integer "$payload" || return
            var_name="MIN_FAN"
            ;;
        max_fan)
            is_integer "$payload" || return
            var_name="MAX_FAN"
            ;;
        target_temp)
            is_integer "$payload" || return
            var_name="TARGET_TEMP"
            ;;
        temp_metric)
            [[ "$payload" =~ ^(max|avg)$ ]] || return
            var_name="TEMP_METRIC"
            ;;
        response_speed)
            [[ "$payload" =~ ^(relaxed|balanced|aggressive)$ ]] || return
            var_name="RESPONSE_SPEED"
            ;;
        *)
            return
            ;;
    esac

    local escaped_payload
    escaped_payload=$(escape_sed_replacement "$payload")
    {
        flock -x 200
        sed -i "s/^${var_name}=.*/${var_name}=${escaped_payload}/" "$STATE_FILE"
    } 200>"${STATE_FILE}.lock"
}

publish_if_changed() {
    local new_pwm=$1
    local last_pwm
    last_pwm=$(cat "$LAST_PWM_FILE" 2>/dev/null || echo "0")

    if [ "$new_pwm" != "$last_pwm" ]; then
        mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" \
            -t "${MQTT_SYSTEM}/fan_speed" -m "$new_pwm" 2>/dev/null || true
        echo "$new_pwm" > "$LAST_PWM_FILE"
    fi
}

cleanup() {
    kill "$MQTT_PID" 2>/dev/null || true
}

# =============================================================================
# Temperature functions
# =============================================================================

get_hdd_temps_fallback() {
    local temps=""
    for dev in "${HDD_DEVICES[@]}"; do
        [ -e "/dev/$dev" ] || continue
        temp=$(timeout 5 smartctl -A "/dev/$dev" 2>/dev/null | awk '/194 Temperature_Celsius/ {print $10}' || echo 0)
        is_integer "$temp" && [ "$temp" -gt 0 ] && temps="$temps $temp"
    done
    echo "$temps" | tr ' ' '\n' | sort -rn | tr '\n' ' ' | sed 's/ *$//' | sed 's/$/:fallback/'
}

get_stale_threshold() {
    local monitor_interval=30
    if [ -f "$MONITOR_INTERVAL_FILE" ]; then
        monitor_interval=$(cat "$MONITOR_INTERVAL_FILE" 2>/dev/null || echo 30)
    fi
    echo $((monitor_interval * 2 + 10))
}

get_hdd_temp_with_age() {
    local file_age=0

    if [ -f "$SHARED_TEMP_FILE" ]; then
        file_age=$(($(date +%s) - $(stat -c %Y "$SHARED_TEMP_FILE" 2>/dev/null || echo 0)))
        local stale_threshold
        stale_threshold=$(get_stale_threshold)

        if [ "$file_age" -lt "$stale_threshold" ]; then
            local temp
            temp=$(cat "$SHARED_TEMP_FILE" 2>/dev/null)
            if [ -z "$temp" ]; then
                log "WARNING: Shared temp file empty, polling HDD temps directly"
                get_hdd_temps_fallback
                return
            fi
            echo "$temp:$file_age"
            return
        else
            log "WARNING: Shared temp file stale (${file_age}s), polling HDD temps directly"
            get_hdd_temps_fallback
            return
        fi
    else
        log "WARNING: Shared temp file missing, polling HDD temps directly"
        get_hdd_temps_fallback
    fi
}

get_temp_for_metric() {
    local temps_with_age=$1
    local temps="${temps_with_age%%:*}"
    local first_temp="${temps%% *}"

    if [ "$TEMP_METRIC" = "avg" ]; then
        echo "$temps" | awk '{sum=0; for(i=1;i<=NF;i++) sum+=$i; printf "%.1f", sum/NF}'
    else
        echo "$first_temp"
    fi
}

update_temp_trend() {
    local current_temp=$1
    local file_mtime=0

    if [ -f "$SHARED_TEMP_FILE" ]; then
        file_mtime=$(stat -c %Y "$SHARED_TEMP_FILE" 2>/dev/null || echo 0)
    fi

    # only sample when temp file has been updated (new data from monitor)
    if [ "$file_mtime" -le "$LAST_TEMP_FILE_MTIME" ]; then
        return
    fi
    LAST_TEMP_FILE_MTIME=$file_mtime

    TEMP_HISTORY="$TEMP_HISTORY $current_temp"
    local count
    count=$(echo "$TEMP_HISTORY" | wc -w)
    [ "$count" -gt "$TEMP_HISTORY_SIZE" ] && TEMP_HISTORY=$(echo "$TEMP_HISTORY" | cut -d' ' -f2-)

    count=$(echo "$TEMP_HISTORY" | wc -w)
    [ "$count" -lt 3 ] && return

    local oldest newest diff
    oldest=$(echo "$TEMP_HISTORY" | cut -d' ' -f1)
    newest=$(echo "$TEMP_HISTORY" | awk '{print $NF}')
    diff=$(awk -v n="$newest" -v o="$oldest" 'BEGIN {printf "%.1f", n - o}')

    if float_compare "$diff" "<=" "$TREND_FALLING_FAST"; then
        PI_TREND_MULT="0"
    elif float_compare "$diff" "<" "$TREND_FALLING"; then
        PI_TREND_MULT="0.2"
    elif float_compare "$diff" "<" "$TREND_STABLE"; then
        PI_TREND_MULT="1.0"
    else
        PI_TREND_MULT="1.5"
    fi
}

get_response_multiplier() {
    case "$RESPONSE_SPEED" in
        relaxed)    echo "0.5" ;;
        balanced)   echo "1.0" ;;
        aggressive) echo "2.0" ;;
        *)          echo "1.0" ;;
    esac
}

# =============================================================================
# PI controller functions
# =============================================================================

reset_pi_controller() {
    PI_INTEGRAL=0
    PI_LAST_PWM=0
    PI_LAST_TIME=0
}

calculate_pwm() {
    local temp=$1 min_temp=$2 max_temp=$3 min_fan=$4 max_fan=$5
    [ "$temp" -le "$min_temp" ] && echo "$min_fan" && return
    [ "$temp" -ge "$max_temp" ] && echo "$max_fan" && return
    awk -v t="$temp" -v t_min="$min_temp" -v t_max="$max_temp" -v f_min="$min_fan" -v f_max="$max_fan" \
        'BEGIN {print int(f_min + (t - t_min) * (f_max - f_min) / (t_max - t_min))}'
}

calculate_target_temp_pwm() {
    local current_temp=$1 target_temp=$2 min_fan=$3 max_fan=$4
    local pi_max_integral=$((max_fan - min_fan))
    local now error p_term new_integral new_pwm baseline dt

    if float_compare "$current_temp" "==" 0; then
        PI_RESULT=$min_fan
        return
    fi

    now=$(date +%s)

    baseline=$min_fan
    error=$(awk -v t="$current_temp" -v target="$target_temp" 'BEGIN {printf "%.1f", t - target}')

    if [ "$PI_LAST_TIME" -eq 0 ]; then
        dt=1
    else
        dt=$((now - PI_LAST_TIME))
        [ "$dt" -lt 1 ] && dt=1
        [ "$dt" -gt 10 ] && dt=10
    fi

    p_term=$(awk -v e="$error" -v kp="$PI_KP" 'BEGIN {printf "%.0f", kp * e}')

    local trend_mult speed_mult final_mult
    update_temp_trend "$current_temp"
    trend_mult=$PI_TREND_MULT
    speed_mult=$(get_response_multiplier)

    # apply trend/speed multipliers when above target
    # decay uses speed_mult only if SYMMETRIC_DECAY is enabled
    local accum_mult decay_mult
    accum_mult=$(awk -v t="$trend_mult" -v s="$speed_mult" 'BEGIN {printf "%.2f", t * s}')
    if $SYMMETRIC_DECAY; then
        decay_mult=$speed_mult
    else
        decay_mult="1.0"
    fi
    PI_TREND_MULT=$trend_mult

    # anti-windup
    if [ "$PI_LAST_PWM" -ge "$max_fan" ] && float_compare "$error" ">" 0; then
        new_integral=$PI_INTEGRAL
    else
        new_integral=$(awk -v i="$PI_INTEGRAL" -v e="$error" -v ki="$PI_KI" -v dt="$dt" -v max="$pi_max_integral" -v am="$accum_mult" -v dm="$decay_mult" \
            'BEGIN {
                if (e > 0) {
                    # Above target: accumulate with trend/speed multiplier
                    ni = i + ki * e * dt * am
                } else {
                    # At/below target: decay (symmetric if enabled)
                    ni = i + ki * e * dt * dm
                }
                if (ni > max) ni = max
                if (ni < 0) ni = 0
                printf "%.2f", ni
            }')
    fi
    PI_INTEGRAL=$new_integral

    new_pwm=$(awk -v b="$baseline" -v p="$p_term" -v i="$PI_INTEGRAL" \
        'BEGIN {printf "%.0f", b + p + i}')

    if [ "$PI_LAST_PWM" -gt 0 ]; then
        local max_change=$((PI_MAX_RATE * dt))
        local diff=$((new_pwm - PI_LAST_PWM))
        [ "$diff" -gt "$max_change" ] && new_pwm=$((PI_LAST_PWM + max_change))
        [ "$diff" -lt "-$max_change" ] && new_pwm=$((PI_LAST_PWM - max_change))
    fi

    [ "$new_pwm" -lt "$min_fan" ] && new_pwm=$min_fan
    [ "$new_pwm" -gt "$max_fan" ] && new_pwm=$max_fan

    PI_LAST_PWM=$new_pwm
    PI_LAST_TIME=$now

    PI_RESULT=$new_pwm
}

# =============================================================================
# Mode handlers
# =============================================================================

# PI Mode transition handlers
handle_target_change() {
    if [ -z "$PREV_TARGET_TEMP" ] || [ "$TARGET_TEMP" = "$PREV_TARGET_TEMP" ]; then
        PREV_TARGET_TEMP=$TARGET_TEMP
        return
    fi

    local current_temp_int
    current_temp_int=$(get_temp_for_metric "$(get_hdd_temp_with_age)")
    current_temp_int=$(temp_to_int "$current_temp_int")

    if [ "$current_temp_int" -lt "$TARGET_TEMP" ]; then
        PI_INTEGRAL=$(awk -v i="$PI_INTEGRAL" -v f="$INTEGRAL_REDUCE_FACTOR" 'BEGIN {printf "%.2f", i * f}')
        log "TARGET CHANGE: ${PREV_TARGET_TEMP}°C → ${TARGET_TEMP}°C, now below target, integral reduced: I:$PI_INTEGRAL"
    fi
    PREV_TARGET_TEMP=$TARGET_TEMP
}

handle_mode_transition() {
    # save PI state when leaving target_temp mode
    if [ "$PREV_FAN_MODE" = "target_temp" ] && [ "$FAN_MODE" != "target_temp" ]; then
        SAVED_PI_INTEGRAL=$PI_INTEGRAL
        SAVED_PI_TIME=$(date +%s)
    fi

    # restore/init PI state when entering target_temp mode
    if [ "$FAN_MODE" = "target_temp" ] && [ "$PREV_FAN_MODE" != "target_temp" ]; then
        reset_pi_controller
        TEMP_HISTORY=""
        LAST_TEMP_FILE_MTIME=0

        # warm start: calculate integral from current hardware PWM
        local current_pwm pi_max_integral_init calculated_integral=0
        current_pwm=$(cat /sys/class/hwmon/hwmon0/pwm1 2>/dev/null || echo 0)
        pi_max_integral_init=$((MAX_FAN - MIN_FAN))

        if [ "$current_pwm" -gt "$MIN_FAN" ]; then
            local current_temp_int temp_info expected_p_term
            temp_info=$(get_hdd_temp_with_age)
            current_temp_int=$(get_temp_for_metric "$temp_info")
            current_temp_int=$(temp_to_int "$current_temp_int")
            expected_p_term=$(( (current_temp_int - TARGET_TEMP) * PI_KP ))
            [ "$expected_p_term" -lt 0 ] && expected_p_term=0
            calculated_integral=$((current_pwm - MIN_FAN - expected_p_term))
            [ "$calculated_integral" -lt 0 ] && calculated_integral=0
            [ "$calculated_integral" -gt "$pi_max_integral_init" ] && calculated_integral=$pi_max_integral_init
        fi

        # restore saved integral if changed back to PI mode within window, otherwise use warm start
        local now elapsed
        now=$(date +%s)
        elapsed=$((now - SAVED_PI_TIME))
        if [ -n "$SAVED_PI_INTEGRAL" ] && [ "$elapsed" -lt "$PI_RESTORE_WINDOW" ]; then
            PI_INTEGRAL=$SAVED_PI_INTEGRAL
            log "MODE SWITCH: Restored saved integral ($SAVED_PI_INTEGRAL) after ${elapsed}s, calculated was ($calculated_integral)"
        else
            [ "$calculated_integral" -gt 0 ] && PI_INTEGRAL=$calculated_integral
            [ -n "$SAVED_PI_INTEGRAL" ] && log "MODE SWITCH: Saved integral expired (${elapsed}s), using calculated ($calculated_integral)"
        fi
        SAVED_PI_INTEGRAL=""
    fi

    PREV_FAN_MODE="$FAN_MODE"
}

# Fan mode implementations (set FAN_PWM_RESULT, print log line)
handle_mode_unas_managed() {
    local temp_info temps max_temp avg_temp file_age drive_count
    temp_info=$(get_hdd_temp_with_age)
    temps="${temp_info%%:*}"
    file_age="${temp_info##*:}"
    max_temp="${temps%% *}"

    avg_temp=$(echo "$temps" | awk '{sum=0; for(i=1;i<=NF;i++) sum+=$i; printf "%.1f", sum/NF}')
    drive_count=$(echo "$temps" | wc -w)

    FAN_PWM_RESULT=$(cat /sys/class/hwmon/hwmon0/pwm1 2>/dev/null || echo 0)

    log "UNAS MANAGED: drives=[${temps// /,}] max=${max_temp}°C avg=${avg_temp}°C (${drive_count} drives, $(format_data_source "$file_age")) → $FAN_PWM_RESULT PWM ($(pwm_to_percent "$FAN_PWM_RESULT"))"
}

handle_mode_custom_curve() {
    local temp_info temps temp file_age
    temp_info=$(get_hdd_temp_with_age)
    temps="${temp_info%%:*}"
    temp="${temps%% *}"
    file_age="${temp_info##*:}"

    FAN_PWM_RESULT=$(calculate_pwm "$temp" "$MIN_TEMP" "$MAX_TEMP" "$MIN_FAN" "$MAX_FAN")
    set_pwm "$FAN_PWM_RESULT"

    log "CUSTOM CURVE MODE: ${temp}°C ($(format_data_source "$file_age")) → $FAN_PWM_RESULT PWM ($(pwm_to_percent "$FAN_PWM_RESULT"))"
}

handle_mode_target_temp() {
    local temp_info file_age temp metric_label

    temp_info=$(get_hdd_temp_with_age)
    file_age="${temp_info##*:}"
    temp=$(get_temp_for_metric "$temp_info")

    # if temp data is too stale, poll drives directly for PI controller
    if [ "$file_age" != "fallback" ] && [ "$file_age" -gt "$STALE_TEMP_THRESHOLD" ]; then
        temp_info=$(get_hdd_temps_fallback)
        file_age="fallback"
        temp=$(get_temp_for_metric "$temp_info")
    fi

    local temp_int
    temp_int=$(temp_to_int "$temp")
    metric_label="max"
    [ "$TEMP_METRIC" = "avg" ] && metric_label="avg"

    if [ "$temp_int" -eq 0 ]; then
        FAN_PWM_RESULT=$MIN_FAN
        set_pwm "$FAN_PWM_RESULT"
        log "TARGET TEMP MODE [$RESPONSE_SPEED]: No drives detected, using min fan ($(pwm_to_percent "$FAN_PWM_RESULT"))"
        return
    fi

    calculate_target_temp_pwm "$temp" "$TARGET_TEMP" "$MIN_FAN" "$MAX_FAN"
    FAN_PWM_RESULT=$PI_RESULT
    set_pwm "$FAN_PWM_RESULT"

    local error status
    error=$(awk -v t="$temp" -v target="$TARGET_TEMP" 'BEGIN {printf "%.1f", t - target}')
    status="at target"
    if float_compare "$error" ">" 0; then
        status="cooling (+${error}°C)"
    elif float_compare "$error" "<" 0; then
        status="warm up (${error}°C)"
    fi

    local data_source decay_mode
    if [ "$file_age" = "fallback" ]; then
        data_source="direct poll"
    else
        data_source="$(printf "%2s" "$file_age")s old"
    fi
    $SYMMETRIC_DECAY && decay_mode="sym" || decay_mode="asym"

    log "TARGET TEMP MODE [$RESPONSE_SPEED/$decay_mode]: ${temp}°C ($metric_label, $data_source) → ${TARGET_TEMP}°C target ($status) → $FAN_PWM_RESULT PWM (I:$PI_INTEGRAL T:$PI_TREND_MULT) ($(pwm_to_percent "$FAN_PWM_RESULT"))"
}

handle_mode_set_speed() {
    set_pwm "$FAN_MODE"
    FAN_PWM_RESULT=$FAN_MODE
    log "SET SPEED MODE: $FAN_PWM_RESULT PWM ($(pwm_to_percent "$FAN_PWM_RESULT"))"
}

reset_to_defaults() {
    log "Invalid mode: $FAN_MODE, defaulting to UNAS Managed"
    {
        echo "FAN_MODE=unas_managed"
        echo "MIN_TEMP=$MIN_TEMP"
        echo "MAX_TEMP=$MAX_TEMP"
        echo "MIN_FAN=$MIN_FAN"
        echo "MAX_FAN=$MAX_FAN"
        echo "TARGET_TEMP=$TARGET_TEMP"
        echo "TEMP_METRIC=$TEMP_METRIC"
        echo "RESPONSE_SPEED=$RESPONSE_SPEED"
    } > "$STATE_FILE"
    reset_pi_controller
}

# =============================================================================
# Main dispatcher
# =============================================================================

set_fan_speed() {
    # shellcheck source=/dev/null
    source "$STATE_FILE"

    handle_target_change
    handle_mode_transition

    FAN_PWM_RESULT=0

    case "$FAN_MODE" in
        unas_managed)
            handle_mode_unas_managed
            ;;
        auto)
            handle_mode_custom_curve
            ;;
        target_temp)
            handle_mode_target_temp
            ;;
        *)
            if is_integer "$FAN_MODE" && [ "$FAN_MODE" -ge 0 ] && [ "$FAN_MODE" -le 255 ]; then
                handle_mode_set_speed
            else
                reset_to_defaults
                return
            fi
            ;;
    esac

    publish_if_changed "$FAN_PWM_RESULT"
}

# =============================================================================
# Initialization
# =============================================================================

# Initialize state file with defaults
{
    echo "FAN_MODE=$FAN_MODE"
    echo "MIN_TEMP=$MIN_TEMP"
    echo "MAX_TEMP=$MAX_TEMP"
    echo "MIN_FAN=$MIN_FAN"
    echo "MAX_FAN=$MAX_FAN"
    echo "TARGET_TEMP=$TARGET_TEMP"
    echo "TEMP_METRIC=$TEMP_METRIC"
    echo "RESPONSE_SPEED=$RESPONSE_SPEED"
} > "$STATE_FILE"

echo "0" > "$LAST_PWM_FILE"

SERVICE=false
[ "${1:-}" = "--service" ] && SERVICE=true

rotate_log_if_needed
log "Fan control service starting..."

# Fetch retained MQTT messages on startup (retry up to 30 times every 2 seconds)
log "Fetching MQTT state..."
MQTT_OUTPUT=""
for i in {1..30}; do
    MQTT_OUTPUT=$(timeout 5 mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" \
        -t "${MQTT_FAN}/mode" \
        -t "${MQTT_FAN}/curve/+" \
        -C 8 \
        -F "%t %p" 2>/dev/null || true)

    if [ -n "$MQTT_OUTPUT" ]; then
        break
    fi

    [ "$i" -lt 30 ] && sleep 2
done

if [ -n "$MQTT_OUTPUT" ]; then
    echo "$MQTT_OUTPUT" | while read -r topic payload; do
        update_state_from_mqtt "$topic" "$payload"
    done
    log "Fan control initialized with MQTT state:"
else
    log "No retained MQTT messages found, using defaults:"
fi

log "$(cat "$STATE_FILE")"

# Publish resolved state back to MQTT with retain so HA entities pick up the
# correct initial values (especially after integration removal cleared topics)
# shellcheck source=/dev/null
source "$STATE_FILE"
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/mode" -m "$FAN_MODE" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/min_temp" -m "$MIN_TEMP" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/max_temp" -m "$MAX_TEMP" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/min_fan" -m "$MIN_FAN" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/max_fan" -m "$MAX_FAN" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/target_temp" -m "$TARGET_TEMP" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/temp_metric" -m "$TEMP_METRIC" 2>/dev/null || true
mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -r \
    -t "${MQTT_FAN}/curve/response_speed" -m "$RESPONSE_SPEED" 2>/dev/null || true
log "Published initial state to MQTT"

# Start persistent MQTT subscription for updates
mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" \
    -t "${MQTT_FAN}/mode" \
    -t "${MQTT_FAN}/curve/+" \
    -F "%t %p" 2>/dev/null | while read -r topic payload; do
    update_state_from_mqtt "$topic" "$payload"
done &
MQTT_PID=$!

trap cleanup EXIT TERM INT

# =============================================================================
# Main loop
# =============================================================================

if $SERVICE; then
    while true; do
        set_fan_speed
        sleep 1
    done
else
    set_fan_speed
fi
