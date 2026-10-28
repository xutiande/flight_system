# -*- coding: utf-8 -*-

import os
from pathlib import Path

# ============================================================
# 路径
# ============================================================
# 本文件位于 <项目根>/csair/settings.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent
INFO_DIR = PROJECT_ROOT / "info"
LOG_DIR = PROJECT_ROOT / "logs"
TOKEN_FILE = INFO_DIR / "token.txt"

# ============================================================
# 数据库
# ============================================================
MYSQL = {
	"host": "127.0.0.1",
	"port": 3306,
	"user": "root",
	"password": "123456",
	"database": "csair",
	"charset": "utf8mb4",
}

MONGO = {
	"host": "127.0.0.1",
	"port": 27017,
	"username": None,
	"password": None,
	"database": "csair",
	"collection": "raw_responses",
	# 用 admin 认证；若账号建在业务库里，改成对应库名
	"auth_source": "admin",
	"max_pool_size": 100,
}

REDIS = {
	"host": "127.0.0.1",
	"port": 6379,
	"broker_db": 6,    # Celery 队列 + 熔断标记
	"backend_db": 7,   # Celery 任务结果
	"cookies_db": 8,   # 采集 cookie（TOKEN / acw_sc__v2）
	"throttle_db": 9,  # 按需采集的负缓存 / 限速 / 并发锁
}

# ============================================================
# HTTP 接口
# ============================================================
# 直接运行 python -m csair.api.main 时读取这里的配置。
# 想换端口就改这里，或者用 uvicorn 命令行覆盖。
API = {
	"host": "127.0.0.1",
	"port": 8082,
}

# ============================================================
# 采集范围
# ============================================================
# 热门城市（IATA 城市三字码）
HOT_CITIES = [
	'URC', 'CTU', 'HGH', 'NKG', 'HRB', 'SZX', 'CSX', 'WUH',
	'CGO', 'SHE', 'SYX', 'SHA', 'CAN', 'SIA', 'CKG', 'HAK',
	'KMG', 'XMN', 'BJS',
]

# 每天生成未来几天（含今天）的任务
DATE_RANGE_DAYS = 7

# ============================================================
# 调度参数
# ============================================================
# 队列里保持多少条待执行任务（按需补货，不一次性塞满）
QUEUE_TARGET_DEPTH = 10

# 单个任务最大重试次数
MAX_RETRIES = 3

# 失败任务重试前的等待时间（分钟）
RETRY_WAIT_MIN = 5

# 任务卡在 queued 超过这么久，认为消息已丢失，回收为 pending
QUEUED_TIMEOUT_MIN = 30

# 每轮解析入库的最大条数
PARSE_LIMIT = 200

# 单次请求后随机休眠区间（秒）
REQUEST_DELAY = (10, 20)

# ============================================================
# 熔断
# ============================================================
# 致命失败原因：再重试也没用，必须人工介入（换 token / 换 IP）
FATAL_REASONS = {
	"token_expired",   # 登录态失效，需更新 info/token.txt
	"ip_blocked",      # 账号或 IP 被风控
	"captcha_failed",  # 验证码服务异常
	"acw_failed",      # 反爬参数拿不到
}

REDIS_PAUSE_KEY = "csair:paused"
PAUSE_TTL = 3600  # 熔断持续 1 小时，或人工清除

# ============================================================
# 按需采集（/search 查不到时同步采一次）
# ============================================================
ON_DEMAND = {
	"enabled": True,

	# 全局限速：一分钟内最多触发几次按需采集。
	# 批采的睡眠是 10~20 秒/次（约 4 次/分钟），
	# 按需没有睡眠，靠这个限速补上风控节奏。
	"rate_limit": 8,
	"rate_window": 60,

	# 同一 (出发, 到达, 日期) 的负缓存：
	# 采到 0 航班后 2 小时内最多再试 2 次，之后直接回「暂无数据」
	"miss_limit": 2,
	"miss_window": 7200,

	# 同一组合的并发锁，防止两个请求同时打接口
	"lock_ttl": 30,

	# 前端等多久再来问
	"retry_after": 5,
}

# ============================================================
# 价格监控
# ============================================================
MONITOR = {
	"enabled": True,

	# 每轮最多派发几条（和 dispatch_pending 的目标深度配合）
	"dispatch_limit": 10,

	# 每天最多消耗多少次监控采集，超出就顺延到明天
	# 批采约 1026 次/天，剩余预算留给监控
	"daily_budget": 500,

	# 分级频率：按距起飞天数决定检查间隔（分钟）
	# 越临近起飞价格越活跃，远期几乎不动
	"intervals": [
		(2, 120),      # 0~2 天：2 小时一次
		(7, 360),      # 3~7 天：6 小时一次
		(9999, 1440),  # 8 天以上：1 天一次
	],

	# 同一规则的通知冷却：24 小时内只发一次
	"notify_cooldown_hours": 24,
	# 除非价格比上次通知时又降了 5%
	"notify_drop_ratio": 0.95,
}

# 邮件凭证文件（发件人账号 + 授权码），格式见 info/mail.txt
MAIL_FILE = INFO_DIR / "mail.txt"

# 邮件默认参数，实际凭证从 MAIL_FILE 读
MAIL_DEFAULTS = {
	"smtp_host": "smtp.qq.com",
	"smtp_port": 465,          # QQ 邮箱 SSL 端口
	"user": "",                # 发件邮箱，如 123456@qq.com
	"auth_code": "",           # SMTP 授权码（不是登录密码）
	"from_name": "航班价格监控",
	"default_to": "",          # 收件人，留空则发给自己
}

# ============================================================
# 采集端
# ============================================================
USER_AGENT = (
	"Mozilla/5.0 (Linux; Android 11; Redmi K20 Pro) AppleWebKit/537.36 "
	"(KHTML, like Gecko) Chrome/78.0.3904.96 Mobile Safari/537.36"
)

# 风控服务地址（获取 acw_sc__v2、校验验证码）
RISK_SERVICE = "http://127.0.0.1:8081"

# 航班查询接口
FLIGHT_API = "https://m.csair.com/CSMBP/bookProcess/avPrice/getAvPrice"


# ============================================================
# 凭证
# ============================================================
def load_token() -> str:
	token = os.getenv("CSAIR_TOKEN")
	if token:
		return token.strip()

	if TOKEN_FILE.exists():
		token = TOKEN_FILE.read_text(encoding="utf-8").strip()
		if token:
			return token

	raise RuntimeError(
		"未找到 TOKEN：请设置环境变量 CSAIR_TOKEN，"
		f"或写入文件 {TOKEN_FILE}"
	)
