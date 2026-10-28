# -*- coding: utf-8 -*-

import redis
from loguru import logger

from csair.assets import city_of_airport
from csair.cookies import load_cookies, save_cookies
from csair.database import MongoDB, MySQLDB
from csair.exceptions import CollectError
from csair.settings import FATAL_REASONS, ON_DEMAND, REDIS, USER_AGENT
from csair.spider import FlightSpider
from csair.storage import parse_one, save_raw

LOCK_KEY = "csair:ondemand:lock:{dep}:{arr}:{date}"
MISS_KEY = "csair:ondemand:miss:{dep}:{arr}:{date}"
RATE_KEY = "csair:ondemand:rate"


def _store():
	"""限速与负缓存专用连接（db 9）"""
	return redis.Redis(
		host=REDIS["host"], port=REDIS["port"], db=REDIS["throttle_db"],
		socket_connect_timeout=2, decode_responses=True,
	)


def _city_code(code: str) -> str:
	"""
	把入参统一成城市码。

	机场码 PKX 要转成城市码 BJS，因为南航接口的 cities 参数收的是城市码。
	"""
	code = (code or "").strip().upper()
	return city_of_airport(code) or code


def _compact_date(value) -> str:
	"""
	统一成南航接口要求的 yyyymmdd。

	为什么要转：
	    /search 对外用 2026-10-31（ISO 格式，前端和 SQL 都方便），
	    但南航接口只认 20261031。带横线传过去会被当成非法日期，
	    返回 B0952「日期太远」——一个看起来完全无关的错误。

	Redis 的 key 也用这个格式，保证 2026-10-31 和 20261031 命中同一个计数器。
	"""
	s = str(value).strip()
	if len(s) == 8 and s.isdigit():
		return s
	return s.replace("-", "")[:8]


def _rollback_miss(store, key: str):
	"""采集失败时把这次尝试退回，不消耗用户的 no_data 配额"""
	try:
		if store.decr(key) <= 0:
			store.delete(key)
	except Exception:
		pass


def _handle_collect_error(store, miss_key, exc, dep_city, arr_city, date_str) -> dict:
	"""
	把采集异常翻译成对外的结果。

	分三类：

	   致命（我们的故障）  token 过期 / 被封 / 验证码 / acw
	       → 回滚配额，可重试。用户不该因为我们的问题被锁 2 小时。

	   业务限制         api_error（如 B0952「日期太远」）
	       → 保留配额，不可重试。这不是我们坏了，是这个日期/航线本来查不到。

	   其它             invalid_city / retry_exhausted / save_failed
	       → 回滚配额，可重试。
	"""
	reason = getattr(exc, "reason", "unknown")
	detail = getattr(exc, "detail", "")

	if reason in FATAL_REASONS:
		_rollback_miss(store, miss_key)
		logger.warning(
			f"[按需] {dep_city}->{arr_city} {date_str} 采集失败 [{reason}] {detail}"
		)
		return {"ok": False, "reason": "upstream_error",
		        "detail": reason, "retry_after": 30}

	if reason == "api_error":
		# 接口明确的业务拒绝，重试也不会变，保留计数避免反复打
		logger.info(
			f"[按需] {dep_city}->{arr_city} {date_str} 接口拒绝：{detail}"
		)
		return {"ok": False, "reason": "api_error", "detail": detail}

	if reason == "invalid_city":
		_rollback_miss(store, miss_key)
		logger.info(f"[按需] {dep_city}->{arr_city} 城市码无效")
		return {"ok": False, "reason": "invalid_city"}

	_rollback_miss(store, miss_key)
	logger.warning(
		f"[按需] {dep_city}->{arr_city} {date_str} 采集失败 [{reason}] {detail}"
	)
	return {"ok": False, "reason": "upstream_error",
	        "detail": reason, "retry_after": 30}


def collect_on_demand(dep: str, arr: str, dep_date) -> dict:
	"""
	同步采集一条航线 + 立即解析入库。

	:param dep: 出发城市码或机场码
	:param arr: 到达城市码或机场码
	:param dep_date: 航班日期 yyyy-MM-dd

	:return: 见下面各分支
	    {"ok": True,  "flights": n, "prices": m}
	    {"ok": False, "reason": "no_flight"}                 采集成功但 0 航班
	    {"ok": False, "reason": "collecting", "retry_after": n}
	    {"ok": False, "reason": "no_data"}                   负缓存已满
	    {"ok": False, "reason": "throttled",  "retry_after": n}
	    {"ok": False, "reason": "upstream_error", "detail": r}
	"""
	cfg = ON_DEMAND
	if not cfg.get("enabled"):
		return {"ok": False, "reason": "disabled"}

	dep_city = _city_code(dep)
	arr_city = _city_code(arr)
	date_str = _compact_date(dep_date)

	store = _store()
	lock_key = LOCK_KEY.format(dep=dep_city, arr=arr_city, date=date_str)
	miss_key = MISS_KEY.format(dep=dep_city, arr=arr_city, date=date_str)

	# ---------- 1) 并发锁 ----------
	try:
		# 给当前的采集条件上锁：dep_city、arr_city、date_str
		got_lock = store.set(lock_key, "1", nx=True, ex=cfg["lock_ttl"])
	except Exception as e:
		logger.warning(f"[按需] Redis 不可用，跳过按需采集：{e}")
		return {"ok": False, "reason": "upstream_error", "detail": "redis_unavailable"}
	# 如果当前锁存在，则返回
	if not got_lock:
		return {"ok": False, "reason": "collecting", "retry_after": cfg["retry_after"]}

	try:
		# ---------- 2) 负缓存 ----------
		#
		miss = int(store.get(miss_key) or 0)
		# 判断当前的采集条件：：dep_city、arr_city、date_str 已经采集几次，
		if miss >= cfg["miss_limit"]:
			logger.info(
				f"[按需] {dep_city}->{arr_city} {date_str} 已试 {miss} 次，直接返回无数据"
			)
			return {"ok": False, "reason": "no_data"}

		# ---------- 3) 全局限速 ----------
		try:
			# RATE_KEY 计数器 + 1
			n = store.incr(RATE_KEY)
			if n == 1:
				# 设置过期时间 60 s
				store.expire(RATE_KEY, cfg["rate_window"])
		except Exception:
			n = 0
		# 如果大于 rate_limit 则触发限速条件
		if n > cfg["rate_limit"]:
			logger.info(f"[按需] 触发全局限速（{n}/{cfg['rate_limit']}）")
			return {"ok": False, "reason": "throttled", "retry_after": cfg["rate_window"]}

		# ---------- 4) 记一次尝试 ----------
		try:
			# miss_key 计数 + 1，并且第一次记录的时候设置过期时间
			if store.incr(miss_key) == 1:
				# 设置 miss_key 的过期时间为 2 小时
				store.expire(miss_key, cfg["miss_window"])
		except Exception:
			pass

		# ---------- 5) 采集 ----------
		try:
			# 采集程序
			parsed = _fetch_and_store(dep_city, arr_city, date_str)
		except CollectError as e:
			return _handle_collect_error(store, miss_key, e, dep_city, arr_city, date_str)
		except Exception:
			_rollback_miss(store, miss_key)
			logger.exception(f"[按需] {dep_city}->{arr_city} {date_str} 未预期异常")
			return {"ok": False, "reason": "upstream_error",
			        "detail": "unexpected", "retry_after": 30}

		# ---------- 6) 结果判定 ----------
		# 如果有数据
		if parsed["flights"] > 0:
			try:
				store.delete(miss_key)  # 采到了，配额清零
			except Exception:
				pass
			return {"ok": True,
			        "flights": parsed["flights"],
			        "prices": parsed["prices"]}

		return {"ok": False, "reason": "no_flight"}

	finally:
		try:
			# 解除锁
			store.delete(lock_key)
		except Exception:
			pass


def _fetch_and_store(dep_city: str, arr_city: str, dep_date: str) -> dict:
	"""
	真正的采集动作：请求 -> 落库 -> 立即解析。

	单独抽成函数有两个好处：
	    1. 测试里可以直接替换，不用真的打接口
	    2. 采集链路和限流/缓存逻辑分开，各自好读

	:return: parse_one 的返回值 {"flights", "prices", "error"}
	:raises CollectError: 采集层给出的明确失败原因
	"""
	db = MySQLDB()
	mongo = MongoDB()

	cookies = load_cookies()
	spider = FlightSpider(cookies, USER_AGENT)
	try:
		# ★ 这里没有 sleep：按需采集不等限速间隔
		payload = spider.fetch_flights(dep_date, dep_city, arr_city)
	finally:
		save_cookies(spider.cookies)
		spider.close()

	sign = save_raw(mongo, payload, dep_city, arr_city, dep_date)
	return parse_one(db, mongo, sign)
