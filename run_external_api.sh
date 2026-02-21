#!/bin/bash
set -e

# ============================================================
#  OASIS 社区模拟 — 一键启动脚本
#
#  用法:
#    1. 复制 .env.example 为 .env，填入你的 API Key 和配置
#    2. bash run_external_api.sh
#
#  依赖管理: uv (自动安装 uv + 创建 venv + 安装依赖)
#  支持平台: openai / deepseek / qwen / openai-compatible
# ============================================================


# 确保不会继承环境中残留的代理设置
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY 2>/dev/null || true

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# ============================================================
#  从 .env 加载配置
# ============================================================
if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "📄 加载配置: .env"
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
else
    echo "⚠️  未找到 .env 文件，请复制 .env.example 为 .env 并填入配置"
    echo "   cp .env.example .env"
    exit 1
fi

# ============================================================
#  读取配置 (从 .env 环境变量获取，支持命令行覆盖)
# ============================================================

LLM_PLATFORM="${OASIS_LLM_PLATFORM:-openai-compatible}"
MODEL_NAME="${OASIS_MODEL_NAME:-}"
API_KEY="${OASIS_API_KEY:-}"
API_URL="${OASIS_API_URL:-}"

NUM_AGENTS="${OASIS_NUM_AGENTS:-5}"
ROUNDS="${OASIS_COMMUNITY_ROUNDS:-3}"
CONTINUOUS="${OASIS_CONTINUOUS:-}"
ROUND_DELAY="${OASIS_ROUND_DELAY:-2.0}"
PLATFORM="${OASIS_PLATFORM:-twitter}"
TEMPERATURE="${OASIS_MODEL_TEMPERATURE:-0.7}"

SCHEDULE="${OASIS_SCHEDULE:-}"
TOPICS_NUM="${OASIS_TOPICS_NUM:-}"
INITIAL_POST="${OASIS_INITIAL_POST:-}"
REFRESH_REC_POST_COUNT="${OASIS_REFRESH_REC_POST_COUNT:-}"
MAX_REC_POST_LEN="${OASIS_MAX_REC_POST_LEN:-}"
FOLLOWING_POST_COUNT="${OASIS_FOLLOWING_POST_COUNT:-}"

EXTERNAL_AGENTS_CONFIG="${OASIS_EXTERNAL_AGENTS_CONFIG:-}"
DARK_AGENTS="${OASIS_DARK_AGENTS:-0}"
DARK_PRESET="${OASIS_DARK_PRESET:-full_dark}"
DARK_EVAL_INTERVAL="${OASIS_DARK_EVAL_INTERVAL:-0}"

VIEWER="${OASIS_VIEWER:-true}"
VIEWER_PORT="${OASIS_VIEWER_PORT:-8001}"

# 校验必填项
if [ -z "$API_KEY" ]; then
    echo "❌ 需要 API Key，请在 .env 中设置 OASIS_API_KEY"
    exit 1
fi
if [ -z "$MODEL_NAME" ]; then
    echo "❌ 需要模型名称，请在 .env 中设置 OASIS_MODEL_NAME"
    exit 1
fi

# ============================================================
#  环境准备 (uv)
# ============================================================

VENV_DIR="$SCRIPT_DIR/.venv"

# uv 缓存放到 /data 分区（根分区空间不足）
export UV_CACHE_DIR="/data/.uv-cache"

# 安装 uv（如果不存在）
if ! command -v uv &>/dev/null; then
    echo "📦 安装 uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 创建 venv（如果不存在）
if [ ! -d "$VENV_DIR" ]; then
    echo "🐍 创建虚拟环境 ($VENV_DIR)..."
    uv venv "$VENV_DIR" --python 3.11
fi

# 激活 venv
source "$VENV_DIR/bin/activate"

# 安装项目依赖
echo "📦 安装/更新依赖..."
uv pip install --no-build-isolation \
    "camel-ai==0.2.78" \
    "pandas==2.2.2" \
    "igraph==0.11.6" \
    "cairocffi==1.7.1" \
    "pillow==10.3.0" \
    "aiosqlite" \
    "aiohttp" \
    "pyyaml" \
    "scikit-learn" \
    "python-dotenv" \
    "neo4j>=5.23.0" \
    || { echo "❌ 依赖安装失败"; exit 1; }

# 以 --no-deps 方式安装 oasis 本身（不触发任何重型依赖）
uv pip install --no-deps -e . || { echo "❌ oasis 安装失败"; exit 1; }
echo "✅ 依赖就绪"
echo ""

# ============================================================
#  启动
# ============================================================

echo "========================================"
echo "  OASIS 社区模拟"
echo "========================================"
echo "  平台:     $LLM_PLATFORM"
echo "  模型:     $MODEL_NAME"
echo "  API URL:  ${API_URL:-平台默认}"
echo "  Agents:   $NUM_AGENTS"
echo "  轮数:     $ROUNDS"
echo "  Schedule: ${SCHEDULE:-无}"
echo "  持续模式: $CONTINUOUS"
echo "  外部Agent: ${EXTERNAL_AGENTS_CONFIG:-无}"
echo "  可视化:   $VIEWER (端口 $VIEWER_PORT)"
echo "  Python:   $(python --version)"
echo "  venv:     $VENV_DIR"
echo "========================================"

# DB 路径（模拟和可视化共用）
DB_PATH="./community_simulation.db"

# ── 启动可视化前端（后台） ──
VIEWER_PID=""
if [ "$VIEWER" = "true" ]; then
    echo "🖥️  启动可视化前端 (端口 $VIEWER_PORT)..."
    python community_viewer/live_server.py --db "$DB_PATH" --port "$VIEWER_PORT" &
    VIEWER_PID=$!
    echo "   PID: $VIEWER_PID"
    echo "   浏览器打开: http://localhost:$VIEWER_PORT"
    echo ""
fi

# 退出时清理可视化进程
cleanup() {
    if [ -n "$VIEWER_PID" ] && kill -0 "$VIEWER_PID" 2>/dev/null; then
        echo ""
        echo "🛑 关闭可视化前端 (PID $VIEWER_PID)..."
        kill "$VIEWER_PID" 2>/dev/null || true
        wait "$VIEWER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# 构建命令
CMD="python community_simulation.py"
CMD="$CMD --llm-platform $LLM_PLATFORM"
CMD="$CMD --model-name $MODEL_NAME"
CMD="$CMD --api-key $API_KEY"
CMD="$CMD --num-agents $NUM_AGENTS"
CMD="$CMD --rounds $ROUNDS"
CMD="$CMD --platform $PLATFORM"
CMD="$CMD --temperature $TEMPERATURE"

CMD="$CMD --db-path $DB_PATH"

if [ -n "$API_URL" ]; then
    CMD="$CMD --api-url $API_URL"
fi

if [ -n "$SCHEDULE" ] && [ -f "$SCHEDULE" ]; then
    CMD="$CMD --schedule $SCHEDULE"
fi

if [ -n "$TOPICS_NUM" ]; then
    CMD="$CMD --topics-num $TOPICS_NUM"
fi

if [ -n "$INITIAL_POST" ]; then
    CMD="$CMD --initial-post \"$INITIAL_POST\""
fi

if [ -n "$REFRESH_REC_POST_COUNT" ]; then
    CMD="$CMD --refresh-rec-post-count $REFRESH_REC_POST_COUNT"
fi

if [ -n "$MAX_REC_POST_LEN" ]; then
    CMD="$CMD --max-rec-post-len $MAX_REC_POST_LEN"
fi

if [ -n "$FOLLOWING_POST_COUNT" ]; then
    CMD="$CMD --following-post-count $FOLLOWING_POST_COUNT"
fi

if [ -n "$CONTINUOUS" ] && [ "$CONTINUOUS" != "0" ] && [ "$CONTINUOUS" != "false" ] && [ "$CONTINUOUS" != "" ]; then
    CMD="$CMD --continuous --round-delay $ROUND_DELAY"
fi

if [ -n "$EXTERNAL_AGENTS_CONFIG" ] && [ -f "$EXTERNAL_AGENTS_CONFIG" ]; then
    CMD="$CMD --external-agents-config $EXTERNAL_AGENTS_CONFIG"
fi

if [ "$DARK_AGENTS" -gt 0 ]; then
    CMD="$CMD --dark-agents $DARK_AGENTS --dark-preset $DARK_PRESET"
    if [ "$DARK_EVAL_INTERVAL" -gt 0 ]; then
        CMD="$CMD --dark-eval-interval $DARK_EVAL_INTERVAL"
    fi
fi

echo ""
echo "▶ $CMD"
echo ""
$CMD
