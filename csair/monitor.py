# -*- coding: utf-8 -*-

import re
from datetime import date, datetime, timedelta

import redis
from loguru import logger

from csair.assets import city_name, city_of_airport, resolve_airports
from csair.database import MySQLDB
from csair.exceptions import InvalidParamError, NotFoundError
from csair.notify import NotifyError, render_alert_email, send_email, send_webhook
from csair.settings import MONITOR, REDIS

BUDGET_KEY = "csair:monitor:budget"

# 收件邮箱的粗校验：只拦明显错的，不做 RFC 5322 全集
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


# ============================================================
# 参数
# ============================================================

def next_interval_min(days_to_departure: int) -> int:
	"""
	距起飞天数 -> 检查间隔（分钟）。

	2 天内：2 小时一次（价格最活跃）
	3~7 天：6 小时一次
	8 天以上：1 天一次
	"""
	for max_days, minutes in MONITOR["intervals"]:
		if days_to_departure <= max_days:
			return minutes
	return MONITOR["intervals"][-1][1]


def _store():
	return redis.Redis(
		host=REDIS["host"], port=REDIS["port"], db=REDIS["throttle_db"],
		socket_connect_timeout=2, decode_responses=True,
	)


def _budget_used() -> int:
	"""今日已消耗的监控采集次数"""
	try:
		return int(_store().get(BUDGET_KEY) or 0)
	except Exception:
		return 0


def _budget_add(n: int = 1):
	try:
		s = _store()
		if s.incrby(BUDGET_KEY, n) == n:
			s.expire(BUDGET_KEY, 86400)
	except Exception:
		pass


# ============================================================
# ① 派发到期规则
# ============================================================

DUE_SQL = """
select id, dep_city, arr_city, target_date, cabin_scope,
       max_price, notify_channel, check_interval_min
from price_monitor
where is_active = 1
  and target_date >= curdate()
  and (next_check_at is null or next_check_at <= now())
order by next_check_at asc
limit %s
"""


def expire_old(db) -> int:
	"""目标日期已过的规则自动停用，否则列表里全是废规则"""
	return db.execute(
		"update price_monitor set is_active = 0 "
		"where is_active = 1 and target_date < curdate()"
	)


def _task_in_flight(db, dep_city, arr_city, target_date) -> bool:
	"""同一航线日期已有在途任务就不重复插"""
	row = db.query_one(
		"""
		select 1 as x from crawl_task
		where dep_city = %s and arr_city = %s and dep_date = %s
		  and status in ('pending', 'queued', 'running')
		limit 1
		""",
		(dep_city, arr_city, target_date),
	)
	return row is not None


def dispatch_due(db, limit: int = None) -> int:
	"""
	把到期的监控规则转成采集任务。

	返回本轮新插入的任务数。

	关键：**按城市对去重**。同一条 (出发,到达,日期) 可能有多条用户规则，
	但只需要采一次。
	"""
	if not MONITOR.get("enabled"):
		return 0

	limit = limit or MONITOR["dispatch_limit"]

	used = _budget_used()
	remaining = MONITOR["daily_budget"] - used
	if remaining <= 0:
		logger.warning(
			f"[监控] 今日预算已用完（{used}/{MONITOR['daily_budget']}），本轮跳过"
		)
		return 0

	rows = db.query(DUE_SQL, (min(limit, remaining),))
	if not rows:
		return 0

	created = 0
	for r in rows:
		days = (r["target_date"] - date.today()).days
		interval = next_interval_min(days)

		# 先把这条规则的下次检查时间推后，避免本轮反复选中
		db.execute(
			"update price_monitor "
			"set next_check_at = now() + interval %s minute, "
			"    check_interval_min = %s "
			"where id = %s",
			(interval, interval, r["id"]),
		)

		if _task_in_flight(db, r["dep_city"], r["arr_city"], r["target_date"]):
			continue   # 已经有人要去采这一条了

		db.execute(
			"insert into crawl_task (dep_city, arr_city, dep_date, task_type, status) "
			"values (%s, %s, %s, 'monitor', 'pending')",
			(r["dep_city"], r["arr_city"], r["target_date"]),
		)
		_budget_add()
		created += 1

	if created:
		logger.info(f"[监控] 派发 {created} 条监控采集任务（今日已用 {used + created}）")
	return created


# ============================================================
# ② 数据入库后检查阈值
# ============================================================

# 取最低价的实现放在 queries 层（只读查询归那里），这里只做别名，
# 免得 monitor 和 API 各写一套 SQL。
from csair.queries import lowest_offer as cheapest_offer  # noqa: E402


def check_monitors(db, dep_city: str, arr_city: str, dep_date) -> int:
	"""
	某条航线的数据刚入库，检查有没有规则的阈值被触发。

	在 storage.parse_one / process_pending 之后调用。

	返回触发并通知成功的条数。任何异常都被吞掉 ——
	监控出问题不能让采集解析失败。
	"""
	try:
		return _check_monitors(db, dep_city, arr_city, dep_date)
	except Exception:
		logger.exception(f"[监控] 检查 {dep_city}->{arr_city} {dep_date} 时异常")
		return 0


def _check_monitors(db, dep_city, arr_city, dep_date) -> int:
	rules = db.query(
		"""
		select * from price_monitor
		where dep_city = %s and arr_city = %s and target_date = %s
		  and is_active = 1
		""",
		(dep_city, arr_city, dep_date),
	)
	if not rules:
		return 0

	db.execute(
		"update price_monitor set last_checked_at = now() "
		"where dep_city = %s and arr_city = %s and target_date = %s and is_active = 1",
		(dep_city, arr_city, dep_date),
	)

	fired = 0
	for rule in rules:
		offer = cheapest_offer(
			db, rule["dep_city"], rule["arr_city"], rule["target_date"],
			rule["cabin_scope"],
		)
		if not offer:
			continue

		if offer["price_total"] > float(rule["max_price"]):
			continue   # 没到阈值

		db.execute(
			"update price_monitor set hit_count = coalesce(hit_count, 0) + 1 "
			"where id = %s",
			(rule["id"],),
		)

		if not _should_notify(rule, offer["price_total"]):
			logger.info(
				f"[监控] 规则 {rule['id']} 已触发但处于冷却期"
				f"（现价 {offer['price_total']:.0f}）"
			)
			continue

		if _send(db, rule, offer):
			fired += 1

	return fired


def _should_notify(rule, price: float) -> bool:
	"""
	24 小时冷却。除非价格比上次通知时又降了 5% 以上。
	"""
	last = rule["last_notified_at"]
	if last is None:
		return True

	cooldown = MONITOR["notify_cooldown_hours"]
	try:
		hours = (datetime.now() - last).total_seconds() / 3600
	except TypeError:
		return True

	if hours >= cooldown:
		return True

	last_price = rule["last_notified_price"]
	if last_price is None:
		return False

	return price < float(last_price) * MONITOR["notify_drop_ratio"]


def _send(db, rule, offer) -> bool:
	"""落库 -> 发送 -> 回写状态。返回是否发送成功"""
	channel = (rule["notify_channel"] or "email").strip().lower()

	alert_id = db.insert(
		"""
		insert into price_alert
		    (monitor_id, dep_city, arr_city, target_date, flight_no,
		     cabin_code, price_total, max_price, channel, status)
		values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
		""",
		(rule["id"], rule["dep_city"], rule["arr_city"], rule["target_date"],
		 offer["flight_no"], offer["cabin_code"], offer["price_total"],
		 rule["max_price"], channel),
	)

	enriched = dict(rule)
	enriched["dep_name"] = city_name(rule["dep_city"])
	enriched["arr_name"] = city_name(rule["arr_city"])

	try:
		if channel == "webhook":
			subject, _ = render_alert_email(enriched, offer)
			send_webhook(rule["notify_url"], {
				"msgtype": "text",
				"text": {"content": subject},
				"offer": offer,
			})
		else:
			subject, html = render_alert_email(enriched, offer)
			# 发给用户在创建规则时自己填的邮箱
			send_email(subject, html, to=rule.get("notify_to") or None)
	except NotifyError as e:
		logger.warning(f"[监控] 规则 {rule['id']} 通知失败：{e}")
		db.execute(
			"update price_alert set status='failed', error_msg=%s where id=%s",
			(str(e)[:500], alert_id),
		)
		return False

	db.execute(
		"update price_alert set status='sent', sent_at=now() where id=%s",
		(alert_id,),
	)
	db.execute(
		"update price_monitor "
		"set last_notified_at = now(), last_notified_price = %s where id = %s",
		(offer["price_total"], rule["id"]),
	)
	logger.success(
		f"[监控] 规则 {rule['id']} 触发通知："
		f"{rule['dep_city']}->{rule['arr_city']} {rule['target_date']} "
		f"{offer['price_total']:.0f} <= {float(rule['max_price']):.0f}"
	)
	return True


# ============================================================
# ③ 规则 CRUD
# ============================================================

def _norm_city(code: str, label: str) -> str:
	"""城市码或机场码统一成城市码"""
	code = (code or "").strip().upper()
	if not code:
		raise InvalidParamError(f"{label}不能为空")

	city = city_of_airport(code) or code
	if not resolve_airports(city):
		raise InvalidParamError(f"无法识别{label}：{code}")
	return city


def _norm_date(value) -> date:
	text = str(value).strip()
	for fmt in ("%Y-%m-%d", "%Y%m%d"):
		try:
			return datetime.strptime(text, fmt).date()
		except ValueError:
			continue
	raise InvalidParamError(f"日期格式不对：{text}")


def _norm_email(value: str) -> str:
	"""
	收件邮箱归一 + 校验。

	用户填自己的邮箱，触发后由系统统一发出（发件人是配置里的 QQ 号）。
	这里只拦明显错误的写法，不追求 RFC 全集 —— 太严格的校验会挡住正常地址。
	"""
	email = (value or "").strip()
	if not email:
		raise InvalidParamError("请填写接收通知的邮箱")

	if not EMAIL_RE.match(email):
		raise InvalidParamError(f"邮箱格式不对：{email}")

	return email


def create_rule(db, dep: str, arr: str, target_date, max_price,
                cabin_scope: str = "Y", notify_channel: str = "email",
                notify_to: str = "", notify_url: str = "",
                user_id: int = 1) -> int:
	"""新建监控规则，返回 id。同一组合重复创建会返回 400"""
	dep_city = _norm_city(dep, "出发地")
	arr_city = _norm_city(arr, "到达地")

	if dep_city == arr_city:
		raise InvalidParamError("出发地和到达地不能相同")

	day = _norm_date(target_date)
	if day < date.today():
		raise InvalidParamError("目标日期已过")

	price = float(max_price)
	if price <= 0:
		raise InvalidParamError("阈值必须大于 0")

	scope = (cabin_scope or "Y").upper()
	if scope not in ("Y", "J", "ALL"):
		raise InvalidParamError("cabin_scope 只能是 Y / J / ALL")

	channel, recipient, url = _norm_channel(notify_channel, notify_to, notify_url)

	exists = db.query_one(
		"""
		select id from price_monitor
		where user_id=%s and dep_city=%s and arr_city=%s
		  and target_date=%s and is_active=1
		limit 1
		""",
		(user_id, dep_city, arr_city, day),
	)
	if exists:
		raise InvalidParamError("这条航线日期已经在监控中")

	interval = next_interval_min((day - date.today()).days)

	return db.insert(
		"""
		insert into price_monitor
		    (user_id, dep_city, arr_city, target_date, cabin_scope,
		     max_price, notify_channel, notify_to, notify_url, is_active,
		     next_check_at, check_interval_min, hit_count)
		values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, now(), %s, 0)
		""",
		(user_id, dep_city, arr_city, day, scope, price,
		 channel, recipient, url, interval),
	)


def _norm_channel(channel: str, notify_to: str, notify_url: str):
	"""
	归一通知渠道，返回 (channel, 收件邮箱, Webhook 地址)。

	    email   -> 必须有 notify_to（用户自己填的收件邮箱）
	    webhook -> 必须有 notify_url

	发件账号是系统配置里的（info/mail.txt），跟收件人无关。
	"""
	channel = (channel or "email").strip().lower()

	# 兼容早期「把地址填在 channel 里」的写法
	if channel.startswith("http"):
		notify_url = channel
		channel = "webhook"

	if channel == "webhook":
		url = (notify_url or "").strip()
		if not url.startswith(("http://", "https://")):
			raise InvalidParamError("Webhook 地址必须以 http:// 或 https:// 开头")
		return "webhook", "", url

	if channel != "email":
		raise InvalidParamError("notify_channel 只能是 email 或 webhook")

	return "email", _norm_email(notify_to), ""


def list_rules(db, include_inactive: bool = False, user_id: int = 1) -> list:
	"""规则列表，附带当前最低价，用户才知道离阈值多远"""
	where = "user_id = %s"
	if not include_inactive:
		where += " and is_active = 1"

	rows = db.query(
		f"select * from price_monitor where {where} "
		f"order by is_active desc, target_date asc, id desc",
		(user_id,),
	)

	result = []
	for r in rows:
		offer = cheapest_offer(
			db, r["dep_city"], r["arr_city"], r["target_date"], r["cabin_scope"],
		)
		current = offer["price_total"] if offer else None
		max_price = float(r["max_price"])

		result.append({
			"id": r["id"],
			"dep_city": r["dep_city"],
			"arr_city": r["arr_city"],
			"dep_name": city_name(r["dep_city"]),
			"arr_name": city_name(r["arr_city"]),
			"target_date": r["target_date"].isoformat(),
			"cabin_scope": r["cabin_scope"],
			"max_price": max_price,
			"current_price": current,
			"gap": round(current - max_price, 2) if current is not None else None,
			"triggered": current is not None and current <= max_price,
			"is_active": bool(r["is_active"]),
			"hit_count": int(r["hit_count"] or 0),
			"check_interval_min": r["check_interval_min"],
			"next_check_at": (r["next_check_at"].strftime("%Y-%m-%d %H:%M")
			                  if r["next_check_at"] else None),
			"last_checked_at": (r["last_checked_at"].strftime("%Y-%m-%d %H:%M")
			                    if r["last_checked_at"] else None),
			"last_notified_at": (r["last_notified_at"].strftime("%Y-%m-%d %H:%M")
			                     if r["last_notified_at"] else None),
			"notify_channel": r["notify_channel"],
			"notify_to": r["notify_to"],
			"notify_url": r["notify_url"],
		})

	return result


def update_rule(db, rule_id: int, **changes) -> bool:
	"""只允许改这几个字段，避免拼 SQL 注入"""
	allowed = {"max_price", "cabin_scope", "notify_channel", "notify_to",
	           "notify_url", "is_active"}
	sets, params = [], []

	for key, value in changes.items():
		if key not in allowed or value is None:
			continue
		if key == "max_price":
			value = float(value)
			if value <= 0:
				raise InvalidParamError("阈值必须大于 0")
		if key == "is_active":
			value = 1 if value else 0
		if key == "cabin_scope":
			value = str(value).upper()
			if value not in ("Y", "J", "ALL"):
				raise InvalidParamError("cabin_scope 只能是 Y / J / ALL")
		if key == "notify_to":
			value = _norm_email(value)
		if key == "notify_channel":
			value = str(value).strip().lower()
			if value not in ("email", "webhook"):
				raise InvalidParamError("notify_channel 只能是 email 或 webhook")
		sets.append(f"{key} = %s")
		params.append(value)

	if not sets:
		return _exists(db, rule_id)

	# 重新启用时立刻检查一次
	if changes.get("is_active"):
		sets.append("next_check_at = now()")

	params.append(rule_id)
	db.execute(f"update price_monitor set {', '.join(sets)} where id = %s",
	           tuple(params))

	# 注意：MySQL 的 rowcount 只统计「实际发生变化的行」，
	# 把 is_active 从 1 改成 1 会返回 0。所以这里不拿 rowcount 判断成败。
	return _exists(db, rule_id)


def _exists(db, rule_id: int) -> bool:
	return db.query_one(
		"select 1 as x from price_monitor where id = %s", (rule_id,)
	) is not None


def delete_rule(db, rule_id: int) -> bool:
	"""软删除：置 is_active=0，保留历史告警记录"""
	if not _exists(db, rule_id):
		return False

	db.execute("update price_monitor set is_active = 0 where id = %s", (rule_id,))
	return True


def list_alerts(db, rule_id: int, limit: int = 20) -> list:
	rows = db.query(
		"""
		select * from price_alert
		where monitor_id = %s
		order by created_at desc
		limit %s
		""",
		(rule_id, limit),
	)
	return [{
		"id": r["id"],
		"flight_no": r["flight_no"],
		"cabin_code": r["cabin_code"],
		"price_total": float(r["price_total"]),
		"max_price": float(r["max_price"]),
		"channel": r["channel"],
		"status": r["status"],
		"error_msg": r["error_msg"],
		"created_at": r["created_at"].strftime("%Y-%m-%d %H:%M"),
		"sent_at": r["sent_at"].strftime("%Y-%m-%d %H:%M") if r["sent_at"] else None,
	} for r in rows]
