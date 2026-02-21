"""
Oasis Agent 社区

支持：
1) 外部 LLM API (OpenAI / DeepSeek / Qwen 等 OpenAI 兼容 API)
2) Twitter / Reddit 平台选择
3) 有限轮次模式 (--rounds N)
4) 持续运行模式 (--continuous)：不断抽取话题 + Agent 自主互动
5) 个性化推荐 (--personalized-recsys)
6) PsySafe 恶意 Agent 注入 (--dark-agents N)
"""

import argparse
import asyncio
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional

# 从 .env 文件加载环境变量（如果存在）
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass


# ── 日志系统 ──
class TeeWriter:
    """同时写到终端和日志文件的 stdout/stderr 替换器。"""

    def __init__(self, original_stream, log_file):
        self.original = original_stream
        self.log_file = log_file

    def write(self, text):
        if text:
            self.original.write(text)
            self.log_file.write(text)
            self.log_file.flush()

    def flush(self):
        self.original.flush()
        self.log_file.flush()

    def fileno(self):
        return self.original.fileno()

    def isatty(self):
        return self.original.isatty()


async def run_dtdd_evaluation(env, configs, dark_agent_ids, round_num):
    """对所有 Agent 执行 DTDD 心理测试并打印结果。"""
    from dark_agent import DTDD_PROMPT, parse_dtdd_response, format_dtdd_result

    print(f"\n🧪 DTDD 心理测试 @ 轮次 {round_num}")
    print("-" * 50)
    results = []
    for i in range(len(configs)):
        agent = env.agent_graph.get_agent(i)
        name = configs[i].get("name", f"Agent_{i}")
        is_dark = i in dark_agent_ids
        try:
            resp = await agent.perform_interview(DTDD_PROMPT)
            parsed = parse_dtdd_response(resp["content"])
            print(format_dtdd_result(i, name, parsed, is_dark))
            results.append({"agent_id": i, "name": name, "is_dark": is_dark,
                            "result": parsed})
        except Exception as e:
            print(f"  Agent {i} ({name}): ❌ 测试失败 - {e}")
            results.append({"agent_id": i, "name": name, "is_dark": is_dark,
                            "result": None, "error": str(e)})

    # 汇总统计
    dark_scores = [r["result"]["darkness_ratio"] for r in results
                   if r["result"] and r["is_dark"]]
    normal_scores = [r["result"]["darkness_ratio"] for r in results
                     if r["result"] and not r["is_dark"]]
    if dark_scores:
        avg_dark = sum(dark_scores) / len(dark_scores)
        print(f"\n  🔴 恶意 Agent 平均黑化率: {avg_dark:.1%} (n={len(dark_scores)})")
    if normal_scores:
        avg_normal = sum(normal_scores) / len(normal_scores)
        print(f"  🟢 正常 Agent 平均黑化率: {avg_normal:.1%} (n={len(normal_scores)})")
    if dark_scores and normal_scores:
        gap = sum(dark_scores) / len(dark_scores) - sum(normal_scores) / len(normal_scores)
        print(f"  📊 差异: {gap:+.1%}")
    print("-" * 50)
    return results


def setup_logging(log_dir: str = "") -> str:
    """初始化日志系统，返回日志文件路径。

    - 所有 print / stdout / stderr 同时 tee 到日志文件
    - OASIS 框架的 logging 输出也会被捕获
    """
    if not log_dir:
        log_dir = os.path.join(os.path.dirname(__file__), "log")
    os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(log_dir, f"community-{timestamp}.log")

    log_file = open(log_path, "a", encoding="utf-8")

    # 替换 stdout / stderr，实现 tee
    sys.stdout = TeeWriter(sys.__stdout__, log_file)
    sys.stderr = TeeWriter(sys.__stderr__, log_file)

    # 同时让 Python logging 也输出到该文件
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(levelname)s - %(asctime)s - %(name)s - %(message)s"
    ))
    logging.getLogger().addHandler(file_handler)

    return log_path


AGENT_CONFIGS: List[Dict[str, str]] = [
    {"user_name": "tech_explorer", "name": "Alice",
     "description": "科技爱好者，喜欢探索新技术", "persona": "对AI和新技术充满热情"},
    {"user_name": "data_scientist", "name": "Bob",
     "description": "数据科学家，专注于机器学习", "persona": "用数据说话"},
    {"user_name": "ai_researcher", "name": "Charlie",
     "description": "AI研究员", "persona": "思考AI的未来和伦理"},
    {"user_name": "startup_founder", "name": "Diana",
     "description": "创业者", "persona": "追求创新和突破"},
    {"user_name": "software_architect", "name": "Eve",
     "description": "软件架构师", "persona": "注重系统设计"},
    {"user_name": "product_manager", "name": "Frank",
     "description": "产品经理", "persona": "以用户需求为导向"},
    {"user_name": "devops_engineer", "name": "Grace",
     "description": "DevOps工程师", "persona": "自动化的忠实信徒"},
    {"user_name": "ux_designer", "name": "Henry",
     "description": "UX设计师", "persona": "以用户为中心"},
    {"user_name": "security_expert", "name": "Ivy",
     "description": "安全专家", "persona": "注重安全细节"},
    {"user_name": "tech_writer", "name": "Jack",
     "description": "技术作家", "persona": "简化复杂技术"},
]


SIMPLE_ROLES = [
    "社区管理员，喜欢发布公告", "AI技术狂热者", "日常生活分享者",
    "潜水员，偶尔点赞", "激进的评论家", "乐于助人的专家",
    "幽默的段子手", "新闻搬运工", "刚注册的新人", "好奇宝宝",
]


# ── 全局停止信号 ──
_stop_requested = False


def _handle_signal(signum, frame):
    global _stop_requested
    _stop_requested = True
    print("\n⏹️  收到停止信号，将在当前轮次结束后优雅退出...")


class DummyTokenCounter:
    def count_tokens_from_messages(self, messages):
        return 0

    def count_tokens(self, text):
        return 0


def build_agent_configs(num_agents: int, use_simple_roles: bool) -> List[Dict[str, str]]:
    configs: List[Dict[str, str]] = []
    if use_simple_roles:
        for i in range(num_agents):
            role_desc = SIMPLE_ROLES[i] if i < len(SIMPLE_ROLES) else "普通社区成员"
            configs.append({
                "user_name": f"user_{i}",
                "name": f"User_{i}",
                "description": f"我是User_{i}，我是一个{role_desc}。",
                "persona": role_desc,
            })
    else:
        configs = [dict(c) for c in AGENT_CONFIGS]
        if num_agents > len(configs):
            for i in range(len(configs), num_agents):
                configs.append({
                    "user_name": f"user_{i}",
                    "name": f"User_{i}",
                    "description": "社区成员",
                    "persona": "普通用户",
                })
        else:
            configs = configs[:num_agents]
    return configs


PLATFORM_TYPE_MAP = {
    "openai": "OPENAI",
    "deepseek": "DEEPSEEK",
    "qwen": "QWEN",
    "openai-compatible": "OPENAI_COMPATIBLE_MODEL",
}


async def create_model(model_type: str, api_url: str, temperature: float,
                       platform_type: str = "openai-compatible", api_key: str = "EMPTY"):
    from camel.models import ModelFactory
    from camel.types import ModelPlatformType

    platform_name = PLATFORM_TYPE_MAP.get(platform_type, "OPENAI_COMPATIBLE_MODEL")
    model_platform = getattr(ModelPlatformType, platform_name, ModelPlatformType.OPENAI_COMPATIBLE_MODEL)

    create_kwargs = dict(
        model_platform=model_platform,
        model_type=model_type,
        api_key=api_key,
        model_config_dict={"temperature": temperature, "max_tokens": 4096},
    )
    # 只要指定了 api_url 就传给 ModelFactory（支持所有平台自定义 URL）
    if api_url:
        create_kwargs["url"] = api_url

    model = ModelFactory.create(**create_kwargs)
    model._token_counter = DummyTokenCounter()
    return model


def load_topics(csv_path: str, field: str = "") -> List[str]:
    """从 CSV 加载全部话题。"""
    if not os.path.exists(csv_path):
        print(f"⚠️ 未找到话题 CSV: {csv_path}")
        return []
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        topic_col = None
        if field and field in df.columns:
            topic_col = field
        elif "source_tweet" in df.columns:
            topic_col = "source_tweet"
        elif "topic_name" in df.columns:
            topic_col = "topic_name"
        if not topic_col:
            print(f"⚠️ 未找到话题列，可用列: {list(df.columns)}")
            return []
        df = df.dropna(subset=[topic_col])
        df[topic_col] = df[topic_col].astype(str)
        df = df[df[topic_col].str.strip() != ""]
        topics = df[topic_col].tolist()
        print(f"📰 已加载 {len(topics)} 条话题")
        return topics
    except Exception as e:
        print(f"⚠️ 读取话题 CSV 失败: {e}")
        return []


class TopicFeeder:
    """话题供给器：从池中不断提供话题，用完后循环 + 打乱。"""

    def __init__(self, topics: List[str], shuffle: bool = True):
        self._all_topics = list(topics)
        self._queue: List[str] = []
        self._shuffle = shuffle
        self._cycle = 0
        self._total_fed = 0
        self._refill()

    def _refill(self):
        self._queue = list(self._all_topics)
        if self._shuffle:
            random.shuffle(self._queue)
        self._cycle += 1

    def get(self, n: int = 1) -> List[str]:
        result = []
        for _ in range(n):
            if not self._queue:
                self._refill()
            if self._queue:
                result.append(self._queue.pop(0))
                self._total_fed += 1
        return result

    @property
    def total_fed(self) -> int:
        return self._total_fed

    @property
    def pool_size(self) -> int:
        return len(self._all_topics)

    @property
    def remaining(self) -> int:
        return len(self._queue)


def print_round_stats(round_num: int, start_time: float, topic_feeder: Optional['TopicFeeder'] = None):
    elapsed = time.time() - start_time
    hours, remainder = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remainder, 60)
    ts = datetime.now().strftime("%H:%M:%S")
    topic_info = ""
    if topic_feeder:
        topic_info = f" | 话题已投放: {topic_feeder.total_fed}/{topic_feeder.pool_size} (cycle {topic_feeder._cycle})"
    print(f"  [{ts}] 轮次 {round_num} 完成 | 运行时间: {hours:02d}:{minutes:02d}:{seconds:02d}{topic_info}")


async def main():
    log_path = setup_logging()
    print(f"🚀 启动 Oasis Agent 社区...")
    print(f"📋 日志文件: {log_path}")

    parser = argparse.ArgumentParser(description="Oasis Agent 社区模拟")

    # 模型相关
    parser.add_argument("--model-name", default=os.environ.get("OASIS_MODEL_NAME", ""),
                        help="模型名称，如 gpt-4o-mini / deepseek-chat / qwen-plus")
    parser.add_argument("--api-url", default=os.environ.get("OASIS_API_URL", ""),
                        help="API 地址 (留空则使用平台默认地址)")
    parser.add_argument("--api-key", default=os.environ.get("OASIS_API_KEY", ""),
                        help="API Key (必填，也可通过 OASIS_API_KEY 或 OPENAI_API_KEY 设置)")
    parser.add_argument("--llm-platform", default=os.environ.get("OASIS_LLM_PLATFORM", "openai-compatible"),
                        choices=list(PLATFORM_TYPE_MAP.keys()),
                        help="LLM 平台类型: openai, deepseek, qwen, openai-compatible(默认)")
    parser.add_argument("--temperature", type=float,
                        default=float(os.environ.get("OASIS_MODEL_TEMPERATURE", "0.7")))

    # 社区配置
    parser.add_argument("--db-path", default=os.environ.get("OASIS_DB_PATH", "./community_simulation.db"))
    parser.add_argument("--num-agents", type=int, default=int(os.environ.get("OASIS_NUM_AGENTS", "10")))
    parser.add_argument("--platform", choices=["twitter", "reddit"],
                        default=os.environ.get("OASIS_PLATFORM", "twitter"))
    parser.add_argument("--recsys-type", choices=["random", "twitter", "reddit"],
                        default=os.environ.get("OASIS_RECSYS_TYPE", ""))
    parser.add_argument("--refresh-rec-post-count", type=int, default=None,
                        help="每次 refresh 从推荐表采样的帖子数 (Twitter默认2, Reddit默认5)")
    parser.add_argument("--max-rec-post-len", type=int, default=None,
                        help="推荐表每用户最多缓存的帖子数 (Twitter默认2, Reddit默认100)")
    parser.add_argument("--following-post-count", type=int, default=None,
                        help="每次 refresh 从关注者获取的帖子数 (Twitter默认3)")
    parser.add_argument("--use-simple-roles", action="store_true",
                        default=os.environ.get("OASIS_SIMPLE_ROLES", "") not in ("", "0", "false", "False"))
    parser.add_argument("--personalized-recsys", action="store_true",
                        default=os.environ.get("OASIS_PERSONALIZED_RECSYS", "") not in ("", "0", "false", "False"))
    parser.add_argument("--initial-post",
                        default=os.environ.get(
                            "OASIS_INITIAL_POST",
                            "🎉 欢迎来到 Oasis Agent 社区！我们是 AI 助手，在这里进行社交互动。"
                        ))

    # 运行模式
    parser.add_argument("--rounds", type=int, default=int(os.environ.get("OASIS_COMMUNITY_ROUNDS", "3")),
                        help="有限轮次模式的轮数（--continuous 时作为检查点间隔）")
    parser.add_argument("--schedule", default=os.environ.get("OASIS_AGENT_SCHEDULE", ""),
                        help="Agent 发言顺序脚本（YAML），按顺序执行指定 Agent")
    parser.add_argument("--continuous", action="store_true",
                        default=os.environ.get("OASIS_CONTINUOUS", "") not in ("", "0", "false", "False"),
                        help="持续运行模式：不断抽取话题 + Agent 自主互动，Ctrl+C 优雅退出")
    parser.add_argument("--round-delay", type=float,
                        default=float(os.environ.get("OASIS_ROUND_DELAY", "2.0")),
                        help="持续模式下每轮之间的间隔秒数")

    # 话题配置
    parser.add_argument("--topics-csv",
                        default=os.environ.get("OASIS_TOPICS_CSV", "data/twitter_dataset/all_topics.csv"))
    parser.add_argument("--topics-field",
                        default=os.environ.get("OASIS_TOPICS_FIELD", ""))
    parser.add_argument("--topics-num", type=int,
                        default=int(os.environ.get("OASIS_TOPICS_NUM", "3")),
                        help="有限模式: 预采样话题数; 持续模式: 无效（使用全部话题池）")
    parser.add_argument("--topics-seed", type=int,
                        default=int(os.environ.get("OASIS_TOPICS_SEED", "42")))
    parser.add_argument("--topics-per-round", type=int,
                        default=int(os.environ.get("OASIS_TOPICS_PER_ROUND", "1")),
                        help="每轮投放的话题数量")
    parser.add_argument("--topic-inject-prob", type=float,
                        default=float(os.environ.get("OASIS_TOPIC_INJECT_PROB", "0.5")),
                        help="持续模式下每轮投放话题的概率 (0~1)")

    # 外部 Agent (OpenAI 兼容模式 — 每个 Agent 独立 API + 人设)
    parser.add_argument("--external-agents-config", type=str, default="",
                        help="外部 Agent 配置 JSON 文件路径。格式:\n"
                             '[{"api_url":"http://host:port/v1","model":"gpt-4o",'
                             '"api_key":"sk-xxx","name":"Alice","user_name":"alice",'
                             '"persona":"一个热爱科技的年轻人","description":"Tech enthusiast",'
                             '"platform_type":"openai-compatible","temperature":0.7}, ...]')
    parser.add_argument("--external-agents", type=str, default="",
                        help="外部 Agent HTTP 端点列表，逗号分隔")
    parser.add_argument("--external-agent-timeout", type=float,
                        default=float(os.environ.get("OASIS_EXTERNAL_AGENT_TIMEOUT", "30")),
                        help="外部 Agent HTTP 调用超时 (秒)")

    # 杂项
    parser.add_argument("--extra-comments", action="store_true",
                        default=os.environ.get("OASIS_EXTRA_COMMENTS", "") not in ("", "0", "false", "False"))
    parser.add_argument("--show-agent-summary", action="store_true",
                        default=os.environ.get("OASIS_SHOW_AGENT_SUMMARY", "") not in ("", "0", "false", "False"))
    parser.add_argument("--check-only", action="store_true")

    # PsySafe 恶意 Agent
    parser.add_argument("--dark-agents", type=int, default=0,
                        help="注入恶意 Agent 的数量 (基于 PsySafe 黑暗人格特质)")
    parser.add_argument("--dark-preset", default="full_dark",
                        choices=["full_dark", "manipulator", "troll", "narcissist",
                                 "anarchist", "betrayer"],
                        help="恶意人格预设 (默认: full_dark 全维度恶意)")
    parser.add_argument("--dark-traits",
                        default=os.environ.get("OASIS_DARK_TRAITS", ""),
                        help="自定义六维特质向量，如 '1,1,0,0,1,0' (覆盖 --dark-preset)")
    parser.add_argument("--list-dark-presets", action="store_true",
                        help="列出所有可用的恶意人格预设并退出")
    parser.add_argument("--dark-eval-interval", type=int, default=0,
                        help="每隔 N 轮对所有 Agent 做 DTDD 心理测试 (0=不测试)")
    parser.add_argument("--dark-seed-posts", type=int, default=2,
                        help="每个恶意 Agent 的 ICL 种子帖数量 (默认: 2)")

    args = parser.parse_args()

    # ── PsySafe 恶意 Agent 预设列表 ──
    if args.list_dark_presets:
        from dark_agent import DARK_TRAIT_PRESETS, get_active_dimensions
        print("\n🔴 可用的恶意人格预设:")
        print("=" * 60)
        for key, info in DARK_TRAIT_PRESETS.items():
            dims = get_active_dimensions(preset=key)
            print(f"  {key:15s} | {info['label']}")
            print(f"  {'':15s} | {info['description']}")
            print(f"  {'':15s} | 激活维度: {', '.join(dims)}")
            print(f"  {'':15s} | 向量: {info['traits']}")
            print()
        return

    # 解析自定义特质向量
    dark_custom_traits = None
    if args.dark_traits.strip():
        try:
            dark_custom_traits = [int(x.strip()) for x in args.dark_traits.split(",")]
            assert len(dark_custom_traits) == 6 and all(v in (0, 1) for v in dark_custom_traits)
        except Exception:
            print("❌ --dark-traits 格式错误，需要 6 个 0/1 值，如 '1,1,0,0,1,0'")
            return

    # ── API Key 解析 ──
    api_key = args.api_key.strip()
    if not api_key:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        print("❌ 需要 API Key。")
        print("请在 .env 文件中设置 OASIS_API_KEY，或使用 --api-key 参数。")
        return

    # ── 同步 Key 给嵌入模型 (camel OpenAIEmbedding 读取 OPENAI_API_KEY) ──
    embedding_api_key = os.environ.get("OASIS_EMBEDDING_API_KEY", "").strip() or api_key
    os.environ["OPENAI_API_KEY"] = embedding_api_key
    embedding_api_url = os.environ.get("OASIS_EMBEDDING_API_URL", "").strip()
    if embedding_api_url:
        os.environ["OPENAI_API_BASE_URL"] = embedding_api_url

    if args.check_only:
        print("✅ 检查完成。")
        return

    # ── 注册信号处理 ──
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    import oasis
    from camel.models import ModelManager
    from oasis import (ActionType, AgentGraph, LLMAction, ManualAction,
                       SocialAgent, UserInfo)
    from oasis.environment.env_action import ExternalAction
    from oasis.scheduling import AgentSchedule, ScheduleError

    model_type = args.model_name.strip()
    if not model_type:
        print("❌ 需要指定 --model-name (如 gpt-4o-mini, deepseek-chat, qwen-plus)。")
        return

    effective_api_url = args.api_url.strip()

    platform_label = args.llm_platform.upper()
    url_info = f" @ {effective_api_url}" if effective_api_url else ""
    print(f"📦 连接外部 API: {model_type} ({platform_label}{url_info})")
    try:
        model = await create_model(
            model_type=model_type,
            api_url=effective_api_url,
            temperature=args.temperature,
            platform_type=args.llm_platform,
            api_key=api_key,
        )
        model_manager = ModelManager(models=[model], scheduling_strategy="round_robin")
        print("✅ 模型连接成功")
    except Exception as e:
        print(f"❌ 模型初始化失败: {e}")
        return

    available_actions = [
        ActionType.LIKE_POST,
        ActionType.LIKE_COMMENT,
        ActionType.DISLIKE_POST,
        ActionType.CREATE_POST,
        ActionType.CREATE_COMMENT,
        ActionType.FOLLOW,
        ActionType.UNFOLLOW,
        ActionType.REPOST,
    ]

    recsys_type = args.recsys_type.strip()
    if not recsys_type:
        recsys_type = "random"

    agent_graph = AgentGraph()
    agents = []
    configs = build_agent_configs(args.num_agents, args.use_simple_roles)

    # ── 追加恶意 Agent 配置 ──
    dark_agent_ids = []
    if args.dark_agents > 0:
        from dark_agent import (build_dark_agent_configs, print_dark_agent_info,
                                build_dark_user_message_prefix)
        dark_configs = build_dark_agent_configs(
            num_dark=args.dark_agents,
            preset=args.dark_preset,
            custom_traits=dark_custom_traits,
        )
        print_dark_agent_info(dark_configs)
        # 恶意 Agent 从正常 Agent 后面编号
        for dc in dark_configs:
            dark_agent_ids.append(len(configs))
            configs.append(dc)

    print(f"👥 创建 {len(configs)} 个 Agents (其中 {len(dark_agent_ids)} 个恶意)...")
    for i, config in enumerate(configs):
        is_dark = config.get("is_dark", False)
        persona = config.get("persona", "")

        user_info = UserInfo(
            user_name=config["user_name"],
            name=config["name"],
            description=config["description"],
            profile={"other_info": {"user_profile": persona}} if persona else None,
            recsys_type=recsys_type,
        )

        # 恶意 Agent 使用定制 system prompt (Layer 1+2) 和每轮强化 (Layer 4)
        dark_sys = config.get("dark_system_prompt") if is_dark else None
        dark_reinforce = build_dark_user_message_prefix() if is_dark else None

        agent = SocialAgent(
            agent_id=i,
            user_info=user_info,
            agent_graph=agent_graph,
            model=model_manager,
            available_actions=available_actions,
            dark_system_prompt=dark_sys,
            dark_reinforcement=dark_reinforce,
        )
        agent_graph.add_agent(agent)
        agents.append(agent)
        tag = " 🔴 [DARK]" if is_dark else ""
        print(f"  - Agent {i}: {agent.user_info.name} (@{config['user_name']}){tag}")

    print("🔗 建立社交网络...")
    for i in range(len(configs)):
        for j in range(len(configs)):
            if i != j and j % 2 == 0:
                agent_graph.add_edge(i, j)
    print("✅ 社交网络构建完成")

    db_path = args.db_path
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)
    os.environ["OASIS_DB_PATH"] = os.path.abspath(db_path)

    print("🌐 创建模拟环境...")
    print(f"db_path {db_path}")

    from oasis.social_platform.platform import Platform
    from oasis.social_platform.channel import Channel

    channel = Channel()
    if args.platform == "twitter":
        _refresh = args.refresh_rec_post_count if args.refresh_rec_post_count is not None else 2
        _max_rec = args.max_rec_post_len if args.max_rec_post_len is not None else 2
        _follow = args.following_post_count if args.following_post_count is not None else 3
        platform_inst = Platform(
            db_path=db_path,
            channel=channel,
            recsys_type=recsys_type,
            refresh_rec_post_count=_refresh,
            max_rec_post_len=_max_rec,
            following_post_count=_follow,
        )
    else:
        _refresh = args.refresh_rec_post_count if args.refresh_rec_post_count is not None else 5
        _max_rec = args.max_rec_post_len if args.max_rec_post_len is not None else 100
        platform_inst = Platform(
            db_path=db_path,
            channel=channel,
            recsys_type=recsys_type,
            allow_self_rating=True,
            show_score=True,
            max_rec_post_len=_max_rec,
            refresh_rec_post_count=_refresh,
        )

    env = oasis.make(
        agent_graph=agent_graph,
        platform=platform_inst,
        database_path=db_path,
    )
    await env.reset()
    print("✅ 环境准备就绪")

    # ── 注册外部 Agent (OpenAI 兼容模式) ──
    external_agent_map = {}  # agent_id -> endpoint
    if args.external_agents_config:
        import json as _json
        config_path = args.external_agents_config
        if not os.path.isabs(config_path):
            config_path = os.path.join(os.getcwd(), config_path)
        with open(config_path, "r", encoding="utf-8") as f:
            ext_configs = _json.load(f)

        print(f"\n🌐 注册 {len(ext_configs)} 个外部 Agent (OpenAI 兼容模式)...")
        ext_start_id = len(configs)
        for idx, ext_cfg in enumerate(ext_configs):
            ext_id = ext_start_id + idx
            ext_user_name = ext_cfg.get("user_name", f"ext_agent_{ext_id}")
            ext_name = ext_cfg.get("name", f"External Agent {ext_id}")
            ext_desc = ext_cfg.get("description", ext_name)
            ext_persona = ext_cfg.get("persona", ext_desc)
            ext_api_url = ext_cfg["api_url"]
            ext_model_type = ext_cfg.get("model", "gpt-4o")
            ext_api_key = ext_cfg.get("api_key", "EMPTY")
            ext_platform = ext_cfg.get("platform_type", "openai-compatible")
            ext_temperature = ext_cfg.get("temperature", args.temperature)

            # 为这个外部 Agent 创建独立的 model backend
            ext_model = await create_model(
                model_type=ext_model_type,
                api_url=ext_api_url,
                temperature=ext_temperature,
                platform_type=ext_platform,
                api_key=ext_api_key,
            )
            ext_model_mgr = ModelManager(
                models=[ext_model], scheduling_strategy="round_robin")

            ext_user_info = UserInfo(
                user_name=ext_user_name,
                name=ext_name,
                description=ext_desc,
                profile={"other_info": {"user_profile": ext_persona}},
                recsys_type=recsys_type,
            )

            ext_agent = SocialAgent(
                agent_id=ext_id,
                user_info=ext_user_info,
                channel=channel,
                agent_graph=agent_graph,
                model=ext_model_mgr,
                available_actions=available_actions,
            )
            agent_graph.add_agent(ext_agent)
            agents.append(ext_agent)
            # 注册到平台 DB
            await ext_agent.env.action.sign_up(ext_user_name, ext_name, ext_desc)
            print(f"  Agent {ext_id}: {ext_name} (@{ext_user_name}) "
                  f"-> {ext_api_url} ({ext_model_type})")
            print(f"    人设: {ext_persona[:80]}{'...' if len(ext_persona) > 80 else ''}")

        # 外部 Agent 也加入社交网络
        total_agents = len(configs) + len(ext_configs)
        for ext_idx in range(ext_start_id, total_agents):
            for j in range(ext_start_id):
                if j % 2 == 0:
                    agent_graph.add_edge(ext_idx, j)
        print(f"✅ {len(ext_configs)} 个外部 Agent 注册完成 (走 LLMAction 路径)")

    # ── 注册外部 Agent (HTTP API 模式) ──
    elif args.external_agents:
        endpoints = [e.strip() for e in args.external_agents.split(",") if e.strip()]
        if endpoints:
            print(f"\n🌐 注册 {len(endpoints)} 个外部 Agent (HTTP API 模式)...")
            ext_start_id = len(configs)  # 外部 Agent ID 从内部 Agent 之后开始
            for idx, endpoint in enumerate(endpoints):
                ext_id = ext_start_id + idx
                ext_user_name = f"ext_agent_{ext_id}"
                ext_name = f"External Agent {ext_id}"
                ext_bio = f"External HTTP Agent @ {endpoint}"

                user_info = UserInfo(
                    name=ext_name,
                    user_name=ext_user_name,
                    description=ext_bio,
                    profile={"other_info": {"user_profile": ext_bio}},
                    recsys_type="twitter" if args.platform == "twitter" else "reddit",
                )
                ext_agent = SocialAgent(
                    agent_id=ext_id,
                    user_info=user_info,
                    channel=channel,
                    agent_graph=agent_graph,
                )
                agent_graph.add_agent(ext_agent)
                # 注册到平台 DB
                await ext_agent.env.action.sign_up(ext_user_name, ext_name, ext_bio)
                external_agent_map[ext_id] = endpoint
                print(f"  Agent {ext_id}: {ext_name} -> {endpoint}")
            print(f"  超时: {args.external_agent_timeout}s")

    schedule = None
    if args.schedule:
        schedule_path = args.schedule
        if not os.path.isabs(schedule_path):
            schedule_path = os.path.join(os.getcwd(), schedule_path)
        try:
            schedule = AgentSchedule.from_file(schedule_path)
            print(f"📜 已加载发言顺序脚本: {schedule_path}")
        except (OSError, ScheduleError) as e:
            print(f"❌ 无法加载发言顺序脚本: {e}")
            return

    # ── 加载话题 ──
    topics_csv_path = (os.path.join(os.path.dirname(__file__), args.topics_csv)
                       if not os.path.isabs(args.topics_csv) else args.topics_csv)
    all_topics = load_topics(topics_csv_path, args.topics_field)

    # ── 发布初始内容 ──
    print("📝 发布初始内容...")
    initial_actions = {
        env.agent_graph.get_agent(0): [
            ManualAction(
                action_type=ActionType.CREATE_POST,
                action_args={"content": args.initial_post}
            )
        ]
    }

    # 初始投放一批话题
    if all_topics:
        random.seed(args.topics_seed)
        init_topics = random.sample(all_topics, min(args.topics_num, len(all_topics)))
        for idx, topic in enumerate(init_topics, start=1):
            initial_actions[env.agent_graph.get_agent(0)].append(
                ManualAction(
                    action_type=ActionType.CREATE_POST,
                    action_args={"content": f"【话题 {idx}】{topic}"}
                )
            )

    await env.step(initial_actions)

    # 首轮额外评论
    if args.extra_comments:
        extra_actions = {
            env.agent_graph.get_agent(1): [
                ManualAction(
                    action_type=ActionType.CREATE_COMMENT,
                    action_args={"post_id": "1",
                                 "content": "太棒了！作为数据科学家，我很期待看到这个社区的互动模式！📊"}
                )
            ],
            env.agent_graph.get_agent(2): [
                ManualAction(
                    action_type=ActionType.CREATE_COMMENT,
                    action_args={"post_id": "1",
                                 "content": "AI 研究员视角：这将是一个研究社交AI行为的好机会！🤖"}
                )
            ],
        }
        await env.step(extra_actions)

    # ── Layer 3: 恶意 Agent ICL 种子帖 ──
    if dark_agent_ids:
        print("🔴 恶意 Agent 发布种子帖 (ICL 引导)...")
        seed_actions = {}
        for dark_id in dark_agent_ids:
            dark_conf = configs[dark_id]
            seed_posts = dark_conf.get("seed_posts", [])[:args.dark_seed_posts]
            if seed_posts:
                seed_actions[env.agent_graph.get_agent(dark_id)] = [
                    ManualAction(
                        action_type=ActionType.CREATE_POST,
                        action_args={"content": post}
                    )
                    for post in seed_posts
                ]
                for sp in seed_posts:
                    print(f"  Agent {dark_id} ({dark_conf['name']}): {sp[:60]}...")
        if seed_actions:
            await env.step(seed_actions)
        print(f"  共 {sum(len(v) for v in seed_actions.values())} 条种子帖已发布")

    # ── 主循环 ──
    topic_feeder = TopicFeeder(all_topics, shuffle=True) if all_topics else None
    start_time = time.time()

    if args.continuous:
        print(f"\n🔄 进入持续运行模式 (Ctrl+C 优雅退出)")
        print(f"   每轮话题投放概率: {args.topic_inject_prob}")
        print(f"   每轮投放话题数: {args.topics_per_round}")
        print(f"   轮间延迟: {args.round_delay}s")
        print("=" * 60)

        round_num = 0
        while not _stop_requested:
            round_num += 1

            # 以一定概率投放新话题（由随机 Agent 发布）
            if topic_feeder and random.random() < args.topic_inject_prob:
                new_topics = topic_feeder.get(args.topics_per_round)
                if new_topics:
                    poster_id = random.randint(0, len(configs) - 1)
                    topic_actions = {
                        env.agent_graph.get_agent(poster_id): [
                            ManualAction(
                                action_type=ActionType.CREATE_POST,
                                action_args={"content": f"【话题 {topic_feeder.total_fed - len(new_topics) + i + 1}】{t}"}
                            )
                            for i, t in enumerate(new_topics)
                        ]
                    }
                    await env.step(topic_actions)

            # 所有 Agent 自主行动（刷新 feed、发帖、评论、点赞等）
            if schedule:
                ordered_actions = schedule.build_actions(env.agent_graph, round_num=round_num)
                if ordered_actions:
                    await env.step_ordered(ordered_actions)
            else:
                actions = {}
                for _, agent in env.agent_graph.get_agents():
                    aid = agent.social_agent_id
                    if aid in external_agent_map:
                        actions[agent] = ExternalAction(
                            endpoint=external_agent_map[aid],
                            timeout=args.external_agent_timeout,
                            extra_context={"round": round_num},
                        )
                    else:
                        actions[agent] = LLMAction()
                await env.step(actions)

            print_round_stats(round_num, start_time, topic_feeder)

            # 定期打印检查点
            if round_num % args.rounds == 0:
                elapsed = time.time() - start_time
                print(f"\n📊 [检查点 @ 轮次 {round_num}] 已运行 {elapsed/60:.1f} 分钟")
                print(f"   数据库: {db_path}")
                # 检查点行为摘要
                try:
                    import sqlite3
                    _conn = sqlite3.connect(db_path)
                    _cur = _conn.cursor()
                    _cur.execute("SELECT action, COUNT(*) FROM trace GROUP BY action ORDER BY COUNT(*) DESC")
                    for act, cnt in _cur.fetchall():
                        print(f"   {act}: {cnt}")
                    _conn.close()
                except Exception:
                    pass
                print("=" * 60)

            # DTDD 心理测试 (持续模式)
            if (dark_agent_ids and args.dark_eval_interval > 0
                    and round_num % args.dark_eval_interval == 0):
                await run_dtdd_evaluation(env, configs, dark_agent_ids,
                                          round_num)

            if args.round_delay > 0:
                await asyncio.sleep(args.round_delay)

    else:
        # 有限轮次模式
        print(f"\n🤖 开始 Agent 交互 ({args.rounds} 轮)...")
        topic_index = 0
        for round_num in range(1, args.rounds + 1):
            if _stop_requested:
                break

            # 每轮投放话题
            if topic_feeder:
                new_topics = topic_feeder.get(args.topics_per_round)
                if new_topics:
                    topic_actions = {
                        env.agent_graph.get_agent(0): [
                            ManualAction(
                                action_type=ActionType.CREATE_POST,
                                action_args={"content": f"【话题 {topic_feeder.total_fed - len(new_topics) + i + 1}】{t}"}
                            )
                            for i, t in enumerate(new_topics)
                        ]
                    }
                    await env.step(topic_actions)

            if schedule:
                ordered_actions = schedule.build_actions(env.agent_graph, round_num=round_num)
                if ordered_actions:
                    await env.step_ordered(ordered_actions)
            else:
                actions = {}
                for _, agent in env.agent_graph.get_agents():
                    aid = agent.social_agent_id
                    if aid in external_agent_map:
                        actions[agent] = ExternalAction(
                            endpoint=external_agent_map[aid],
                            timeout=args.external_agent_timeout,
                            extra_context={"round": round_num},
                        )
                    else:
                        actions[agent] = LLMAction()
                await env.step(actions)
            print_round_stats(round_num, start_time, topic_feeder)

            # DTDD 心理测试 (有限模式)
            if (dark_agent_ids and args.dark_eval_interval > 0
                    and round_num % args.dark_eval_interval == 0):
                await run_dtdd_evaluation(env, configs, dark_agent_ids,
                                          round_num)

    # ── 最终 DTDD 测试 (模拟结束时) ──
    if dark_agent_ids and args.dark_eval_interval > 0:
        print("\n🧪 最终 DTDD 心理测试 (模拟结束)")
        await run_dtdd_evaluation(env, configs, dark_agent_ids,
                                  round_num=-1)

    # ── 结束 ──
    print("\n📊 社区统计:")
    print("=" * 40)
    for i in range(len(configs)):
        agent = env.agent_graph.get_agent(i)
        tag = " 🔴 [DARK]" if i in dark_agent_ids else ""
        print(f"Agent {i}: {agent.user_info.name} (@{agent.user_info.user_name}){tag}")
        if args.show_agent_summary:
            print(f"  - 描述: {agent.user_info.description}")
            if agent.user_info.profile and "other_info" in agent.user_info.profile:
                persona = agent.user_info.profile["other_info"].get("user_profile", "")
                if persona:
                    preview = persona[:100] + ("..." if len(persona) > 100 else "")
                    print(f"  - Persona: {preview}")

    # DB 行为摘要
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT action, COUNT(*) FROM trace GROUP BY action ORDER BY COUNT(*) DESC")
        rows = cur.fetchall()
        conn.close()
        if rows:
            print("\n📈 行为摘要:")
            for action, cnt in rows:
                print(f"   {action}: {cnt}")
    except Exception:
        pass

    await env.close()
    total_time = time.time() - start_time
    print(f"\n✅ 模拟完成！")
    print(f"   数据库: {db_path}")
    print(f"   日志文件: {log_path}")
    print(f"   总运行时间: {total_time/60:.1f} 分钟")
    if topic_feeder:
        print(f"   话题投放总数: {topic_feeder.total_fed}")


if __name__ == "__main__":
    asyncio.run(main())
