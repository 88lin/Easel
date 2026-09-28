#!/usr/bin/env bash
set -euo pipefail

# Easel — OpenClaw gateway 管理脚本（隔离模式）
# 所有操作通过 --profile easel 隔离，不影响用户自己的 OpenClaw
# 用法: ./scripts/gateway.sh {start|stop|restart|status|logs}

PROFILE="easel"
OC="openclaw --profile $PROFILE"
LOGFILE="/tmp/easel-gateway.log"
ADAPTER_LOGFILE="/tmp/easel-openai-maas-adapter.log"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# ---- gateway 端口：不写死，问 Easel 的解析器 --------------------------------
# OpenClaw 对**非默认 profile** 不用 18789：它按 20000 + fnv1a32(profile) % 40000 分配
# （easel → 37289），并把结果落进 ~/.openclaw-easel/openclaw.json。这里以前写死 18789，
# 于是 healthz 恒探不通：start 每次 --force 重启一个健康的 gateway，status 永远报未运行。
# 解析优先级（环境变量 > openclaw.json > profile 哈希）与 OpenClaw 自己的
# resolveGatewayPort 逐条对齐 —— 单一真相源在 easel/gateway_endpoint.py，别在这里再抄一份。
GATEWAY_PORT="$(
    PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
        'from easel.gateway_endpoint import resolve_gateway_port as p; print(p())' \
        2>/dev/null | tail -n 1
)" || true
# 解析不出来就明着失败，而不是退到一个猜的端口："探错端口"就是这次要根治的病
# （silent wrong-port → healthz 恒假 → start 反复 --force 重启健康的 gateway）。
if ! [[ "$GATEWAY_PORT" =~ ^[0-9]+$ ]]; then
    echo "[easel] 无法解析 gateway 端口（需要 python3，且能 import easel）。" >&2
    echo "        手动查：PYTHONPATH=\"$PROJECT_ROOT\" python3 -c 'from easel.gateway_endpoint import describe; print(describe())'" >&2
    exit 1
fi

# Easel 自己的端口覆盖要透传给 OpenClaw：gateway 进程只认 OPENCLAW_GATEWAY_PORT（它优先级最高），
# 不透传的话 `EASEL_GATEWAY_PORT=xxx ./scripts/gateway.sh start` 会让我们探 xxx、它却听别的。
if [ -n "${EASEL_GATEWAY_PORT:-}" ]; then
    export OPENCLAW_GATEWAY_PORT="$EASEL_GATEWAY_PORT"
fi

# ---- 跨平台兼容（macOS 没有 ss/setsid/procfs）--------------------------
# ss/setsid 属 iproute2/util-linux，/proc 是 Linux 专属；macOS/BSD 三者都没有。
# 按可用性回退，让 gateway.sh 在 Linux 与 macOS 上都能起停。
_port_pid() {
    # 取监听指定端口的进程 PID。Linux 用 ss，macOS/BSD 用 lsof。找不到返回空、退出码 0。
    local port="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -ltnp 2>/dev/null \
            | sed -n "s/.*127\\.0\\.0\\.1:${port}.*pid=\\([0-9][0-9]*\\).*/\\1/p" \
            | head -n 1 || true
    elif command -v lsof >/dev/null 2>&1; then
        lsof -nP -iTCP:"${port}" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
    fi
    return 0
}

_detach() {
    # 后台剥离启动子进程，使其在脚本退出后继续运行。
    # Linux 用 setsid -f（新会话）；macOS 无 setsid，用 nohup + disown。
    # 调用方的 `>LOG 2>&1` 重定向会被子进程继承。
    if command -v setsid >/dev/null 2>&1; then
        setsid -f "$@"
    else
        nohup "$@" &
        disown 2>/dev/null || true
    fi
}

_cmdline() {
    # 取进程命令行，用于 kill 前核对身份。Linux 用 /proc，macOS/BSD 用 ps。
    local pid="$1"
    if [ -r "/proc/$pid/cmdline" ]; then
        tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true
    else
        ps -p "$pid" -o command= 2>/dev/null || true
    fi
    return 0
}

gateway_live() {
    curl -sf --max-time 2 "http://localhost:${GATEWAY_PORT}/healthz" > /dev/null 2>&1
}

gateway_pid() {
    _port_pid "$GATEWAY_PORT"
}

adapter_port() {
    # .env 不存在（未安装/首次运行）时 sed 会失败 → 加 || true，不让 set -e 掐断 stop/status。
    sed -n 's/^OPENAI_MAAS_ADAPTER_PORT=//p' "$PROJECT_ROOT/.env" 2>/dev/null | tail -n 1 || true
}

adapter_pid() {
    local port="${1:-18791}"
    _port_pid "$port"
}

start_adapter() {
    grep -q '^OPENAI_MAAS_API_KEY=' "$PROJECT_ROOT/.env" 2>/dev/null || return 0
    local port pid
    port="$(adapter_port)"
    port="${port:-18791}"
    pid="$(adapter_pid "$port")"
    if [ -n "$pid" ]; then
        return 0
    fi
    _detach /usr/bin/python3 "$PROJECT_ROOT/scripts/openai_maas_adapter.py" \
        --env-file "$PROJECT_ROOT/.env" --port "$port" >"$ADAPTER_LOGFILE" 2>&1
    for _ in $(seq 1 20); do
        curl -sf --max-time 1 "http://127.0.0.1:${port}/health" >/dev/null && return 0
        sleep 0.25
    done
    echo "[easel] OpenAI MaaS adapter may not be ready — check: $ADAPTER_LOGFILE"
}

stop_adapter() {
    local port pid cmdline
    port="$(adapter_port)"
    port="${port:-18791}"
    pid="$(adapter_pid "$port")"
    [ -n "$pid" ] || return 0
    cmdline="$(_cmdline "$pid")"
    if [[ "$cmdline" == *"openai_maas_adapter.py"* ]]; then
        kill "$pid" 2>/dev/null || true
    fi
}

# ---- 项目根（供委派的 shared 脚本按 env 定位 outputs/，见 manifest.py/log.py）----
# workspace 拍平副本按 __file__ 会把根算成 ~/.openclaw，故用 env 钉死正确项目根
export EASEL_ROOT="$PROJECT_ROOT"

# ---- 外网代理 ----
# 公司开发机需要走正向代理才能访问外网（weibo/bilibili/douyin 等）
# no_proxy 排除内网，避免影响 LLM proxy 和内部服务
export http_proxy="${http_proxy:-${EASEL_PROXY:-}}"
export https_proxy="${https_proxy:-${EASEL_PROXY:-}}"
export no_proxy="${no_proxy:-localhost,127.0.0.1,*.xiaohongshu.com,*.devops.xiaohongshu.com,10.*}"

case "${1:-status}" in
    start)
        start_adapter
        if gateway_live; then
            PID="$(gateway_pid)"
            echo "[easel] Gateway already running${PID:+ (PID $PID)}"
            exit 0
        fi
        echo "[easel] Starting Easel gateway (profile: $PROFILE, port: $GATEWAY_PORT)..."
        # 原始事件流由 gateway 进程按自己的 env 写到单个共享文件（web/app.py 会 tail 它做流式）。
        # 注意：`openclaw agent` 客户端没有 --raw-stream 标志，在客户端 env 上设这俩变量无效，
        # 必须在这里、真正跑模型的 gateway 上开启。setsid -f/nohup 会继承下面 export 的 env。
        export OPENCLAW_RAW_STREAM=1
        export OPENCLAW_RAW_STREAM_PATH="${EASEL_RAW_STREAM_PATH:-/tmp/easel-raw-stream.jsonl}"
        : > "$OPENCLAW_RAW_STREAM_PATH"    # 每次起 gateway 清空，避免无限增长/读到上次残留
        # 技能（如 skill-douyin-upload）靠 printenv EASEL_ASKUSER_CARDS 决定用选项卡片还是文字问答。
        # web 的 cli 路径是每轮往客户端 env 里塞这个值，但 http 直连路径下 agent 跑在**本进程**里、
        # 拿不到那份 env —— 不在这里补，2026.9.x 上会从卡片模式悄悄退化成文字问答。该值只取决于
        # OpenClaw 版本有没有 question.* RPC，进程级导出一次即可。
        export EASEL_ASKUSER_CARDS="$(
            PYTHONPATH="$PROJECT_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -c \
                'from easel.gateway_questions import question_bridge_supported as s; print("1" if s() else "0")' \
                2>/dev/null | tail -n 1)"
        export EASEL_ASKUSER_CARDS="${EASEL_ASKUSER_CARDS:-0}"   # 探不出来就按"没有卡片"走文字问答
        _detach openclaw --profile "$PROFILE" gateway run --force --allow-unconfigured --bind loopback > "$LOGFILE" 2>&1
        sleep 4
        if gateway_live; then
            PID="$(gateway_pid)"
            echo "[easel] Gateway started${PID:+ (PID $PID)}"
        else
            echo "[easel] Gateway may not be ready yet — check: tail -f $LOGFILE"
        fi
        ;;
    stop)
        PID="$(gateway_pid)"
        if [ -n "$PID" ] && kill "$PID" 2>/dev/null; then
            echo "[easel] Gateway stopped"
        else
            echo "[easel] Gateway was not running"
        fi
        stop_adapter
        ;;
    restart)
        "$0" stop
        sleep 2
        "$0" start
        ;;
    status)
        if gateway_live; then
            PID="$(gateway_pid)"
            HEALTH=$(curl -sf "http://localhost:${GATEWAY_PORT}/healthz" 2>&1 || echo '{"ok":false}')
            echo "[easel] Gateway running${PID:+ (PID $PID)}, profile: $PROFILE, port: $GATEWAY_PORT"
            echo "  health: $HEALTH"
        else
            echo "[easel] Gateway not running (profile: $PROFILE, port: $GATEWAY_PORT)"
        fi
        ;;
    logs)
        tail -f "$LOGFILE"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
