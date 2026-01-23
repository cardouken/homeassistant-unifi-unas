#!/bin/bash

set -euo pipefail

MQTT_HOST="REPLACE_ME"
MQTT_USER="REPLACE_ME"
MQTT_PASS="REPLACE_ME"
MQTT_ROOT="REPLACE_ME"
MQTT_SYSTEM="${MQTT_ROOT}/system"
MQTT_CONTROL="${MQTT_ROOT}/control"
MQTT_FAN="${MQTT_CONTROL}/fan"

HDD_DEVICES=(sda sdb sdc sdd sde sdf sdg)

STATE_FILE="/tmp/fan_control_state"
LAST_PWM_FILE="/tmp/fan_control_last_pwm"
SHARED_TEMP_FILE="/tmp/unas_hdd_temp"
MONITOR_INTERVAL_FILE="/tmp/unas_monitor_interval"

FAN_MODE="unas_managed"
MIN_TEMP=40
MAX_TEMP=50
MIN_FAN=64
MAX_FAN=255
TARGET_TEMP=42
TEMP_METRIC="max"

PI_INTEGRAL=0
PI_LAST_PWM=0
PI_LAST_TIME=0
PI_RESULT=0
PI_TREND_MULT="1.0"
TREND_RESULT="1.0"
PREV_FAN_MODE=""
PREV_TARGET_TEMP=""

PI_KP=10
PI_KI=0.05
PI_MAX_RATE=5

TEMP_HISTORY=""
TEMP_HISTORY_SIZE=6
LAST_TEMP_SAMPLE=0

RESPONSE_SPEED="balanced"

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

update_state_from_mqtt() {
    local topic=$1 payload=$2
    local var_name
    
    case "${topic##*/}" in
        mode)
            var_name="FAN_MODE"
            ;;
        min_temp)
            [[ "$payload" =~ ^[0-9]+$ ]] || return
            var_name="MIN_TEMP"
            ;;
        max_temp)
            [[ "$payload" =~ ^[0-9]+$ ]] || return
            var_name="MAX_TEMP"
            ;;
        min_fan)
            [[ "$payload" =~ ^[0-9]+$ ]] || return
            var_name="MIN_FAN"
            ;;
        max_fan)
            [[ "$payload" =~ ^[0-9]+$ ]] || return
            var_name="MAX_FAN"
            ;;
        target_temp)
            [[ "$payload" =~ ^[0-9]+$ ]] || return
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
    
    sed -i "s/^${var_name}=.*/${var_name}=${payload}/" "$STATE_FILE"
}

# fetch retained MQTT messages on startup (retry up to 30 times every 2 seconds in case MQTT connection not ready yet)
echo "Fetching MQTT state..."
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
    echo "Fan control initialized with MQTT state:"
else
    echo "No retained MQTT messages found, using defaults:"
fi

cat "$STATE_FILE"

# start persistent MQTT subscription for updates
mosquitto_sub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" \
    -t "${MQTT_FAN}/mode" \
    -t "${MQTT_FAN}/curve/+" \
    -F "%t %p" 2>/dev/null | while read -r topic payload; do
    update_state_from_mqtt "$topic" "$payload"
done &
MQTT_PID=$!

cleanup() {
    kill "$MQTT_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

get_max_hdd_temp_fallback() {
    local max=0 temp
    for dev in "${HDD_DEVICES[@]}"; do
        [ -e "/dev/$dev" ] || continue
        temp=$(timeout 5 smartctl -A "/dev/$dev" 2>/dev/null | awk '/194 Temperature_Celsius/ {print $10}' || echo 0)
        [[ "$temp" =~ ^[0-9]+$ ]] && [ "$temp" -gt "$max" ] && max=$temp
    done
    echo "$max"
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
            temp=$(cat "$SHARED_TEMP_FILE" 2>/dev/null || get_max_hdd_temp_fallback)
            echo "$temp:$file_age"
            return
        else
            echo "WARNING: Shared temp file stale (${file_age}s), polling HDD temps directly" >&2
            temp=$(get_max_hdd_temp_fallback)
            echo "$temp:fallback"
            return
        fi
    else
        echo "WARNING: Shared temp file missing, polling HDD temps directly" >&2
        local temp
        temp=$(get_max_hdd_temp_fallback)
        echo "$temp:fallback"
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
    local now
    now=$(date +%s)

    if [ $((now - LAST_TEMP_SAMPLE)) -lt 10 ]; then
        return
    fi
    LAST_TEMP_SAMPLE=$now

    TEMP_HISTORY="$TEMP_HISTORY $current_temp"
    local count
    count=$(echo $TEMP_HISTORY | wc -w)
    [ "$count" -gt "$TEMP_HISTORY_SIZE" ] && TEMP_HISTORY=$(echo $TEMP_HISTORY | cut -d' ' -f2-)

    count=$(echo $TEMP_HISTORY | wc -w)
    [ "$count" -lt 3 ] && return

    local oldest newest diff
    oldest=$(echo $TEMP_HISTORY | cut -d' ' -f1)
    newest=$(echo $TEMP_HISTORY | awk '{print $NF}')
    diff=$(awk -v n="$newest" -v o="$oldest" 'BEGIN {printf "%.1f", n - o}')

    if awk -v d="$diff" 'BEGIN {exit !(d <= -1.5)}'; then
        TREND_RESULT="0"
    elif awk -v d="$diff" 'BEGIN {exit !(d < 0)}'; then
        TREND_RESULT="0.2"
    elif awk -v d="$diff" 'BEGIN {exit !(d > -0.3 && d < 0.3)}'; then
        TREND_RESULT="1.0"
    else
        TREND_RESULT="1.5"
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

    if awk -v t="$current_temp" 'BEGIN {exit !(t == 0)}'; then
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
    trend_mult=$TREND_RESULT
    speed_mult=$(get_response_multiplier)

    # only apply trend/speed multipliers when above target; normal decay rate at/below target
    if awk -v e="$error" 'BEGIN {exit !(e > 0)}'; then
        final_mult=$(awk -v t="$trend_mult" -v s="$speed_mult" 'BEGIN {printf "%.2f", t * s}')
    else
        final_mult="1.0"
    fi
    PI_TREND_MULT=$trend_mult

    # anti-windup
    if [ "$PI_LAST_PWM" -ge "$max_fan" ] && awk -v e="$error" 'BEGIN {exit !(e > 0)}'; then
        new_integral=$PI_INTEGRAL
    else
        new_integral=$(awk -v i="$PI_INTEGRAL" -v e="$error" -v ki="$PI_KI" -v dt="$dt" -v max="$pi_max_integral" -v mult="$final_mult" \
            'BEGIN {
                if (e > 0) {
                    # Above target: accumulate with trend multiplier
                    ni = i + ki * e * dt * mult
                } else {
                    # At/below target: natural decay (no artificial rates)
                    ni = i + ki * e * dt
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

reset_pi_controller() {
    PI_INTEGRAL=0
    PI_LAST_PWM=0
    PI_LAST_TIME=0
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

set_pwm() {
    echo "$1" > /sys/class/hwmon/hwmon0/pwm1
    echo "$1" > /sys/class/hwmon/hwmon0/pwm2
}

set_fan_speed() {
    # shellcheck source=/dev/null
    source "$STATE_FILE"

    # detect target temperature change and reduce integral if now below target
    if [ -n "$PREV_TARGET_TEMP" ] && [ "$TARGET_TEMP" != "$PREV_TARGET_TEMP" ]; then
        local current_temp_int
        current_temp_int=$(get_temp_for_metric "$(get_hdd_temp_with_age)")
        current_temp_int=${current_temp_int%.*}
        if [ "$current_temp_int" -lt "$TARGET_TEMP" ]; then
            # below new target - reduce integral to 25%
            PI_INTEGRAL=$(awk -v i="$PI_INTEGRAL" 'BEGIN {printf "%.2f", i * 0.25}')
            echo "TARGET CHANGE: ${PREV_TARGET_TEMP}°C → ${TARGET_TEMP}°C, now below target, integral reduced: I:$PI_INTEGRAL"
        fi
    fi
    PREV_TARGET_TEMP=$TARGET_TEMP

    local pwm

    if [ "$FAN_MODE" = "target_temp" ] && [ "$PREV_FAN_MODE" != "target_temp" ]; then
        reset_pi_controller
        TEMP_HISTORY=""
        LAST_TEMP_SAMPLE=0

        # warm start from current PWM
        local current_pwm pi_max_integral_init
        current_pwm=$(cat /sys/class/hwmon/hwmon0/pwm1 2>/dev/null || echo 0)
        pi_max_integral_init=$((MAX_FAN - MIN_FAN))
        if [ "$current_pwm" -gt "$MIN_FAN" ]; then
            local current_temp_int temp_info expected_p_term
            temp_info=$(get_hdd_temp_with_age)
            current_temp_int=$(get_temp_for_metric "$temp_info")
            current_temp_int=${current_temp_int%.*}
            expected_p_term=$(( (current_temp_int - TARGET_TEMP) * PI_KP ))
            [ "$expected_p_term" -lt 0 ] && expected_p_term=0
            local estimated_integral=$((current_pwm - MIN_FAN - expected_p_term))
            [ "$estimated_integral" -gt "$pi_max_integral_init" ] && estimated_integral=$pi_max_integral_init
            [ "$estimated_integral" -gt 0 ] && PI_INTEGRAL=$estimated_integral
        fi
    fi
    PREV_FAN_MODE="$FAN_MODE"

    if [ "$FAN_MODE" = "unas_managed" ]; then
        local temp_info temps max_temp avg_temp file_age drive_count
        temp_info=$(get_hdd_temp_with_age)
        temps="${temp_info%%:*}"
        file_age="${temp_info##*:}"
        max_temp="${temps%% *}"  # first temp (sorted descending)

        # calculate average and count drives
        avg_temp=$(echo "$temps" | awk '{sum=0; for(i=1;i<=NF;i++) sum+=$i; printf "%.1f", sum/NF}')
        drive_count=$(echo "$temps" | wc -w)

        pwm=$(cat /sys/class/hwmon/hwmon0/pwm1 2>/dev/null || echo 0)

        if [ "$file_age" = "fallback" ]; then
            echo "UNAS MANAGED: drives=[${temps// /,}] max=${max_temp}°C avg=${avg_temp}°C (${drive_count} drives, direct poll) → $pwm PWM ($((pwm * 100 / 255))%)"
        else
            echo "UNAS MANAGED: drives=[${temps// /,}] max=${max_temp}°C avg=${avg_temp}°C (${drive_count} drives, ${file_age}s old) → $pwm PWM ($((pwm * 100 / 255))%)"
        fi

    elif [ "$FAN_MODE" = "auto" ]; then
        local temp_info temps temp file_age
        temp_info=$(get_hdd_temp_with_age)
        temps="${temp_info%%:*}"
        temp="${temps%% *}"  # get first (max) temperature
        file_age="${temp_info##*:}"

        pwm=$(calculate_pwm "$temp" "$MIN_TEMP" "$MAX_TEMP" "$MIN_FAN" "$MAX_FAN")
        set_pwm "$pwm"

        if [ "$file_age" = "fallback" ]; then
            echo "CUSTOM CURVE MODE: ${temp}°C (direct poll) → $pwm PWM ($((pwm * 100 / 255))%)"
        else
            echo "CUSTOM CURVE MODE: ${temp}°C (${file_age}s old) → $pwm PWM ($((pwm * 100 / 255))%)"
        fi

    elif [ "$FAN_MODE" = "target_temp" ]; then
        local temp_info file_age temp metric_label
        temp_info=$(get_hdd_temp_with_age)
        file_age="${temp_info##*:}"
        temp=$(get_temp_for_metric "$temp_info")
        temp_int=${temp%.*}
        metric_label="max"
        [ "$TEMP_METRIC" = "avg" ] && metric_label="avg"

        if [ "$temp_int" -eq 0 ]; then
            pwm=$MIN_FAN
            set_pwm "$pwm"
            echo "TARGET TEMP MODE: No drives detected, using min fan ($((pwm * 100 / 255))%)"
        else
            calculate_target_temp_pwm "$temp" "$TARGET_TEMP" "$MIN_FAN" "$MAX_FAN"
            pwm=$PI_RESULT
            set_pwm "$pwm"

            local error
            error=$(awk -v t="$temp" -v target="$TARGET_TEMP" 'BEGIN {printf "%.1f", t - target}')
            local status="at target"
            if awk -v e="$error" 'BEGIN {exit !(e > 0)}'; then
                status="cooling (+${error}°C)"
            elif awk -v e="$error" 'BEGIN {exit !(e < 0)}'; then
                status="warm up (${error}°C)"
            fi

            if [ "$file_age" = "fallback" ]; then
                echo "TARGET TEMP MODE: ${temp}°C ($metric_label) → ${TARGET_TEMP}°C target ($status) → $pwm PWM (I:$PI_INTEGRAL T:$PI_TREND_MULT) ($((pwm * 100 / 255))%)"
            else
                echo "TARGET TEMP MODE: ${temp}°C ($metric_label, $(printf "%2s" "$file_age")s old) → ${TARGET_TEMP}°C target ($status) → $pwm PWM (I:$PI_INTEGRAL T:$PI_TREND_MULT) ($((pwm * 100 / 255))%)"
            fi
        fi

    elif [[ "$FAN_MODE" =~ ^[0-9]+$ ]] && [ "$FAN_MODE" -ge 0 ] && [ "$FAN_MODE" -le 255 ]; then
        set_pwm "$FAN_MODE"
        pwm=$FAN_MODE
        echo "SET SPEED MODE: $pwm PWM ($((pwm * 100 / 255))%)"

    else
        echo "Invalid mode: $FAN_MODE, defaulting to UNAS Managed"
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
        return
    fi

    publish_if_changed "$pwm"
}

if $SERVICE; then
    while true; do
        set_fan_speed
        sleep 1
    done
else
    set_fan_speed
fi
