# -*- coding: utf-8 -*-

import json
import random
import time
from datetime import datetime, timedelta
from itertools import permutations

import redis
from loguru import logger

from csair.celery_app import app
from csair.database import MongoDB, MySQLDB
from csair.exceptions import CollectError
from csair import monitor
from csair.settings import (
	DATE_RANGE_DAYS,
	FATAL_REASONS,
	HOT_CITIES,
	MAX_RETRIES,
	PAUSE_TTL,
	QUEUED_TIMEOUT_MIN,
	QUEUE_TARGET_DEPTH,
	REDIS,
	REDIS_PAUSE_KEY,
	REQUEST_DELAY,
	RETRY_WAIT_MIN,
	USER_AGENT,
	load_token,
)
from csair.spider import FlightSpider
from csair.storage import process_pending, save_raw

# ============================================================
# 进程内单例
# ============================================================
# Celery worker 是长驻进程，每个进程只建一次连接即可，
# 避免每个任务都新建连接池。

_db = None
_mongo = None


def get_db() -> MySQLDB:
	global _db
	if _db is None:
		_db = MySQLDB()
	return _db


def get_mongo() -> MongoDB:
	global _mongo
	if _mongo is None:
		_mongo = MongoDB()
		_mongo.ensure_indexes()
	return _mongo


# ============================================================
# Cookie 缓存
# ============================================================
# 实现在 csair/cookies.py，本文件只是复用。
# 单独抽出去的原因：api/ 和 ondemand/ 也要读写 cookie，
# 但它们不能 import 本文件（会连带拉起 Celery）。
from csair.cookies import load_cookies, save_cookies  # noqa: F401


# ============================================================
# 熔断
# ============================================================

def _redis():
	"""熔断标记用独立连接。和 broker 同库，但 key 不冲突"""
	return redis.Redis(
		host=REDIS["host"], port=REDIS["port"], db=REDIS["broker_db"],
		socket_connect_timeout=2,
	)


def _breaker_reason():
	"""返回熔断原因，未熔断返回 None"""
	try:
		value = _redis().get(REDIS_PAUSE_KEY)
	except Exception as e:
		logger.error(f"[熔断] 读取标记失败：{e}")
		return None
	if not value:
		return None
	return value.decode("utf-8", errors="ignore") \
		if isinstance(value, bytes) else str(value)


def _trip_breaker(reason: str):
	"""触发熔断：暂停后续派发"""
	try:
		_redis().set(REDIS_PAUSE_KEY, reason, ex=PAUSE_TTL)
	except Exception as e:
		logger.error(f"[熔断] 写入标记失败：{e}")


def _mark_failure(db, task_id, retry_count, reason, detail=""):
	"""
	记录一次失败，返回 'failed' 或 'retry'。

	致命原因直接 failed + 熔断，不浪费 2 次重试。
	"""
	msg = f"{reason}: {detail}"[:500] if detail else reason
	new_retry = retry_count + 1

	if reason in FATAL_REASONS:
		db.execute(
			"update crawl_task set status='failed', retry_count=%s, error_msg=%s, "
			"finished_at=now() where id=%s",
			(new_retry, msg, task_id),
		)
		_trip_breaker(reason)
		logger.error(
			f"[采集] 任务 {task_id} 因 <{reason}> 失败 → 已熔断 {PAUSE_TTL // 60} 分钟"
		)
		logger.error(
			f"[采集] 处理：更新 info/token.txt 后执行 "
			f"redis-cli -n {REDIS['broker_db']} del {REDIS_PAUSE_KEY}"
		)
		return "failed"

	if new_retry >= MAX_RETRIES:
		db.execute(
			"update crawl_task set status='failed', retry_count=%s, error_msg=%s, "
			"finished_at=now() where id=%s",
			(new_retry, msg, task_id),
		)
		logger.error(f"[采集] 任务 {task_id} 重试 {new_retry} 次仍失败：{msg}")
		return "failed"

	db.execute(
		"update crawl_task set status='pending', retry_count=%s, error_msg=%s "
		"where id=%s",
		(new_retry, msg, task_id),
	)
	logger.warning(f"[采集] 任务 {task_id} 第 {new_retry} 次失败：{msg}")
	return "retry"


# ============================================================
# ① 生成采集任务
# ============================================================

@app.task
def generate_collect_tasks():
	"""
	城市对 x 未来 N 天 -> crawl_task(status='pending')

	幂等：同一天（date(created_at)=curdate()）相同
	(dep_city, arr_city, dep_date) 已生成过则跳过。
	"""
	db = get_db()
	today = datetime.now().date()

	# 新批次开始，清掉上一轮遗留的熔断标记（token 应该已经换新）
	try:
		_redis().delete(REDIS_PAUSE_KEY)
		logger.info("[生成任务] 已清除熔断标记")
	except Exception as e:
		logger.warning(f"[生成任务] 清除熔断标记失败：{e}")

	sql = """
		insert into crawl_task (dep_city, arr_city, dep_date, task_type, status)
		select %s, %s, %s, 'search', 'pending' from dual
		where not exists (
			select 1 from crawl_task
			where dep_city = %s and arr_city = %s and dep_date = %s
			  and date(created_at) = curdate()
		)
	"""

	created = 0
	with db.cursor() as cur:
		for dep, arr in permutations(HOT_CITIES, 2):
			for offset in range(DATE_RANGE_DAYS):
				d = today + timedelta(days=offset)
				cur.execute(sql, (dep, arr, d, dep, arr, d))
				created += cur.rowcount

	logger.info(f"[生成任务] 今日新增 {created} 条采集任务")
	return created


# ============================================================
# ② 派发
# ============================================================

@app.task
def dispatch_pending(target: int = QUEUE_TARGET_DEPTH):
	"""
	按目标深度补货：把 pending 原子地转为 queued 后投递。

	为什么按深度补货而不是一次派完：
	    worker 每分钟约消费 4 条，一次投 1000 条会让任务在队列里滞留数小时，
	    既浪费内存，也会被回收逻辑误判为卡死。

	queued 中间态：
	    派发后状态立刻变 queued，下一轮不会重复选中同一批任务。

	回收：
	    worker 崩溃导致消息丢失时，任务会停在 queued。
	    超过 QUEUED_TIMEOUT_MIN 且从未开始执行，回退为 pending。
	"""
	db = get_db()

	# ---------- 熔断检查 ----------
	reason = _breaker_reason()
	if reason:
		logger.warning(
			f"[派发] 熔断中（{reason}），本轮不派发。"
			f"修复后执行 redis-cli -n {REDIS['broker_db']} del {REDIS_PAUSE_KEY}"
		)
		return 0

	# ---------- 回收卡死的 queued ----------
	reaped = db.execute(
		"""
		update crawl_task
		set status = 'pending', dispatched_at = null
		where status = 'queued'
		  and started_at is null
		  and coalesce(dispatched_at, created_at) <= now() - interval %s minute
		""",
		(QUEUED_TIMEOUT_MIN,),
	)
	if reaped:
		logger.warning(f"[派发] 回收 {reaped} 条卡在 queued 的任务")

	# ---------- 过期任务：dep_date 早于今天，航班已飞 ----------
	expired = db.execute(
		"""
		update crawl_task
		set status = 'expired', finished_at = now(),
		    error_msg = 'dep_date 已过期，未采集'
		where status in ('pending', 'queued')
		  and dep_date < curdate()
		"""
	)
	if expired:
		logger.warning(f"[派发] 过期 {expired} 条任务（dep_date 早于今天）")

	# ---------- 队列里已有多少待执行 ----------
	row = db.query_one(
		"select count(*) as n from crawl_task where status = 'queued'"
	)
	depth = int(row["n"]) if row else 0
	if depth >= target:
		return 0

	# ---------- 只补差额 ----------
	rows = db.query(
		"""
		select id, dep_city, arr_city, dep_date, retry_count
		from crawl_task
		where status = 'pending'
		  and dep_date >= curdate()
		  and (started_at is null
		       or started_at <= now() - interval %s minute)
		order by (task_type = 'monitor') desc, id asc
		limit %s
		""",
		(RETRY_WAIT_MIN, target - depth),
	)
	if not rows:
		return 0

	# ---------- 逐条原子抢占后投递 ----------
	dispatched = 0
	for r in rows:
		n = db.execute(
			"update crawl_task set status='queued', dispatched_at=now() "
			"where id=%s and status='pending'",
			(r["id"],),
		)
		if n != 1:
			continue  # 被别的进程抢先改状态了

		crawl_one.delay(
			r["id"], r["dep_city"], r["arr_city"],
			r["dep_date"].strftime("%Y%m%d"), r["retry_count"],
		)
		dispatched += 1

	if dispatched:
		logger.info(f"[派发] 投递 {dispatched} 条任务")
	return dispatched


# ============================================================
# ③ 单条采集
# ============================================================

@app.task(bind=True)
def crawl_one(self, task_id: int, dep_city: str, arr_city: str,
              dep_date: str, retry_count: int = 0):
	"""
	单条采集。

	抢占：queued -> running（CAS，保证同一条只执行一次）
	失败：交给 _mark_failure 判断是重试还是熔断
	"""
	db = get_db()
	mongo = get_mongo()

	# ---------- 日期校验：防止跨零点后仍采已过期航班 ----------
	today_str = datetime.now().strftime("%Y%m%d")
	if dep_date < today_str:
		db.execute(
			"update crawl_task set status='expired', finished_at=now(), "
			"error_msg='dep_date 已过期，未采集' "
			"where id=%s and status in ('pending', 'queued')",
			(task_id,),
		)
		logger.warning(
			f"[采集] 任务 {task_id} 日期 {dep_date} 已过期（今天 {today_str}），跳过"
		)
		return "expired"

	# ---------- 原子抢占 ----------
	claimed = db.execute(
		"update crawl_task set status='running', started_at=now() "
		"where id=%s and status='queued'",
		(task_id,),
	)
	if claimed == 0:
		logger.info(f"[采集] 任务 {task_id} 状态不是 queued，跳过")
		return "skipped"

	try:
		# cookie 从 Redis 取（含上一次刷新出来的 acw_sc__v2）
		cookies = load_cookies()

		spider = FlightSpider(cookies, USER_AGENT)
		try:
			payload = spider.fetch_flights(dep_date, dep_city, arr_city)
		finally:
			# 采集过程中可能换过 acw cookie，无论成功失败都回写，
			# 下一个任务就不用再走一次挑战
			save_cookies(spider.cookies)
			spider.close()

		# 原始响应落地，拿到幂等键
		sign = save_raw(mongo, payload, dep_city, arr_city, dep_date)

		db.execute(
			"update crawl_task set status='success', finished_at=now(), sign=%s "
			"where id=%s",
			(sign, task_id),
		)
		logger.success(f"[采集] {dep_city}->{arr_city} {dep_date} 成功 sign={sign}")

		# 请求间隔，避免触发风控
		time.sleep(random.uniform(*REQUEST_DELAY))
		return "ok"

	except CollectError as e:
		# 采集层给出了明确原因
		return _mark_failure(db, task_id, retry_count, e.reason, e.detail)

	except Exception as e:
		logger.exception(f"[采集] 任务 {task_id} 未预期异常")
		return _mark_failure(db, task_id, retry_count, "unexpected", str(e))


# ============================================================
# ④ 解析入库
# ============================================================
@app.task
def parse_pending(limit: int = 200):
	"""
	扫 Mongo 里 parse_status='waiting' 的响应，解析后写入 MySQL。

	两阶段状态分离：
	    raw_responses.parse_status  解析阶段（waiting/success/failed）
	    crawl_task.status           采集阶段（pending/queued/running/success/failed）
	"""
	stat = process_pending(get_db(), get_mongo(), limit=limit)
	if stat["total"]:
		logger.info(
			f"[入库] 本轮 {stat['ok']}/{stat['total']} 条，"
			f"航班 {stat['flights']} / 舱位 {stat['prices']}，失败 {stat['failed']}"
		)
	return stat["ok"]


# ============================================================
# ⑤ 价格监控派发
# ============================================================

@app.task
def dispatch_monitor():
	"""
	把到期的价格监控规则转成采集任务。

	每 5 分钟跑一次。真正采集仍走 crawl_task → dispatch_pending → crawl_one，
	限速、熔断、重试都是现成的。
	"""
	db = get_db()

	expired = monitor.expire_old(db)
	if expired:
		logger.info(f"[监控] {expired} 条规则因目标日期已过自动停用")

	created = monitor.dispatch_due(db)
	return created
