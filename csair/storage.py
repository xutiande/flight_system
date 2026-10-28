# -*- coding: utf-8 -*-
from datetime import datetime

from loguru import logger

from csair.assets import KNOWN_AIRPORTS, airport_name, iter_airports, iter_cities
from csair.exceptions import CollectError
from csair.monitor import check_monitors
from csair.parser import parseSelectFight
from csair.utils import ZipData, formatDateStr, hourBucket, make_sign


# ============================================================
# SQL
# ============================================================

# 城市主数据
SQL_UPSERT_CITY = """
insert into city (city_code, name_cn, country, province, airport_count)
values (%s, %s, %s, %s, %s)
on duplicate key update
    name_cn       = values(name_cn),
    country       = values(country),
    province      = coalesce(values(province), province),
    airport_count = values(airport_count)
"""

# 机场主数据
SQL_UPSERT_AIRPORT = """
insert into airport (iata, name_cn, city_code, country)
values (%s, %s, %s, %s)
on duplicate key update
    name_cn   = values(name_cn),
    city_code = values(city_code),
    country   = values(country)
"""

# 航线：命中即置 active，并把下次采集时间往后推
SQL_UPSERT_ROUTE = """
insert into route (dep_iata, arr_iata, status, source,
                   hit_count, miss_count, last_verified_at, next_run_at)
values (%s, %s, 'active', 'emergent', 1, 0, now(), now() + interval 1440 minute)
on duplicate key update
    status           = 'active',
    hit_count        = hit_count + 1,
    miss_count       = 0,
    last_verified_at = now(),
    next_run_at      = now() + interval fetch_interval minute
"""

SQL_SELECT_ROUTE_ID = "select id from route where dep_iata = %s and arr_iata = %s"

# 航班：只存静态属性，价格与余票在 price_snapshot
SQL_UPSERT_FLIGHT = """
insert into flight (
    flight_no, dep_date, dep_iata, arr_iata, arr_date,
    dep_time, arr_time, duration_min, aircraft, aircraft_name,
    dep_term, arr_term, stop_count, is_code_share,
    marketing_flight_no, meal, lowest_price
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
          %s, %s, %s, %s, %s, %s, %s)
on duplicate key update
    dep_iata            = values(dep_iata),
    arr_iata            = values(arr_iata),
    arr_date            = values(arr_date),
    dep_time            = values(dep_time),
    arr_time            = values(arr_time),
    duration_min        = values(duration_min),
    aircraft            = values(aircraft),
    aircraft_name       = values(aircraft_name),
    dep_term            = values(dep_term),
    arr_term            = values(arr_term),
    stop_count          = values(stop_count),
    is_code_share       = values(is_code_share),
    marketing_flight_no = values(marketing_flight_no),
    meal                = values(meal),
    lowest_price        = values(lowest_price)
"""

# 舱位价格：唯一键含 fare_basis，同一舱位代码下的不同产品才能共存
SQL_UPSERT_PRICE = """
insert into price_snapshot (
    flight_no, dep_date, cabin_code, fare_basis, cabin_class,
    base_cabin, price, tax, price_total, seats_left,
    seats_is_lower_bound, refund_change_type, captured_at
) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
on duplicate key update
    cabin_class          = values(cabin_class),
    base_cabin           = values(base_cabin),
    price                = values(price),
    tax                  = values(tax),
    price_total          = values(price_total),
    seats_left           = values(seats_left),
    seats_is_lower_bound = values(seats_is_lower_bound),
    refund_change_type   = values(refund_change_type)
"""

# 采集任务：%s 数量必须与下面的参数元组一致（当前 9 个）
SQL_INSERT_TASK = """
insert into crawl_task (route_id, dep_city, arr_city, dep_date, task_type,
                        status, raw_items, parsed_rows, price_rows,
                        started_at, finished_at)
values (%s, %s, %s, %s, %s, 'success', %s, %s, %s, %s, now())
"""


# ============================================================
# 行转换
# ============================================================

def _flight_row(f):
	return (
		f["flight_no"],
		f["dep_date"],
		f["dep_iata"],
		f["arr_iata"],
		f["arr_date"],
		f["dep_time"],
		f["arr_time"],
		f["duration_min"],
		f["aircraft"],
		f["aircraft_name"],
		f["dep_term"],
		f["arr_term"],
		f["stop_count"],
		int(bool(f["is_code_share"])),
		f["marketing_flight_no"],
		f["meal"],
		f["lowest_price"],
	)


def _price_row(p, captured_at):
	return (
		p["flight_no"],
		p["dep_date"],
		p["cabin_code"],
		p["fare_basis"],
		p["cabin_class"],
		p["base_cabin"],
		p["price"],
		p["tax"],
		p["price_total"],
		p["seats_left"],
		int(bool(p["seats_is_lower_bound"])),
		p["refund_change_type"],
		captured_at,
	)


# ============================================================
# 主数据同步
# ============================================================

def seed_master_data(db) -> dict:
	"""
	同步城市与机场主数据。每次启动都跑，upsert 幂等。

	以前用「表里有数据就跳过」判断，导致改了字典也不生效；
	现在改为无条件同步，几百条的写入是毫秒级。
	"""
	# ---------- 一致性校验 ----------
	cities = dict(iter_cities())
	airports = {code: info for code, info in iter_airports()}
	errors = []

	for code, a in airports.items():
		cid = a.get("cityCode")
		if cid not in cities:
			errors.append(f"机场 {code} 的 cityCode={cid} 不在 CityCodeData 中")

	for cid, c in cities.items():
		airport_list = c.get("airports") or []
		for ap in airport_list:
			acode = ap.get("airCode")
			if acode and acode not in airports:
				errors.append(f"城市 {cid} 的机场 {acode} 不在 AirPortCodeData 中")
		if c.get("airportCount") != len(airport_list):
			errors.append(
				f"城市 {cid} 的 airportCount={c.get('airportCount')} "
				f"但 airports 实际 {len(airport_list)} 个"
			)

	if errors:
		raise ValueError("主数据校验失败：\n" + "\n".join(errors[:20]))

	# ---------- 写入 ----------
	city_rows = [
		(cid,
		 c["cityCnName"],
		 c.get("countryCnName") or "中国",
		 None,  # 数据源没有省份
		 c.get("airportCount") or len(c.get("airports") or []))
		for cid, c in cities.items()
	]
	airport_rows = [
		(code, a["airportCnName"], a["cityCode"], a.get("countryCnName") or "中国")
		for code, a in airports.items()
	]

	with db.cursor() as cur:
		cur.executemany(SQL_UPSERT_CITY, city_rows)
		cur.executemany(SQL_UPSERT_AIRPORT, airport_rows)

	logger.info(f"主数据同步：{len(city_rows)} 城市 / {len(airport_rows)} 机场")
	return {"cities": len(city_rows), "airports": len(airport_rows)}


# ============================================================
# 原始响应落地
# ============================================================

def save_raw(mongo, payload: dict, dep_city: str, arr_city: str, dep_date: str) -> str:
	"""
	把采集到的原始响应压缩后写入 MongoDB，返回 sign。

	payload 来自 spider.fetch_flights()：
	    {"json":..., "http_status":..., "data_is_none":...}
	"""
	now = datetime.now()
	hour = now.strftime("%Y%m%d%H")
	sign = make_sign(dep_date, dep_city, arr_city, hour)

	zipResult = ZipData.zipData(payload["json"])
	if not zipResult:
		raise CollectError("save_failed", "gzip 压缩失败")

	doc = {
		"dep_date": dep_date,
		"dep_city": dep_city,
		"arr_city": arr_city,
		"http_status": payload["http_status"],
		"data_is_none": payload["data_is_none"],
		"parse_status": "waiting",
		"parse_error": None,
		"parsed_at": None,
		"sign": sign,
		"captured_at": now,  # TTL 索引字段
	}
	doc.update(zipResult)

	mongo.upsertOne(doc)
	return sign


def decode_raw(doc: dict):
	"""
	把 MongoDB 文档解压 + 解析成结构化结果。

	返回 (result, error)：
	    result 是 dict，error 是 None
	    失败时 result 为 None，error 为原因字符串
	"""
	unzip = ZipData.unZip(doc)
	if not unzip:
		return None, "解压失败"

	if not unzip["status"]:
		return None, "解压后大小与记录不一致"

	result = parseSelectFight(unzip["body"])
	result.update({
		"dep_city": doc.get("dep_city"),
		"arr_city": doc.get("arr_city"),
		"dep_date": doc.get("dep_date"),
		"sign": doc.get("sign"),
	})
	return result, None


# ============================================================
# 结构化入库
# ============================================================

def save_result(db, result: dict, dep_date: str = None,
                record_task: bool = True) -> tuple:
	"""
	把解析结果写进 MySQL。整段在一个事务里，失败全部回滚。

	return (航班数, 舱位行数, {机场对: route_id})

	record_task:
	    True  —— 手工单跑时用，自己建一条 crawl_task
	    False —— Celery 流程里用，任务记录已由 crawl_one 创建，
	             再插会产生 sign 为 NULL 的孤儿行
	"""
	flights = result.get("flights") or []
	prices = result.get("prices") or []
	dep_city = result.get("dep_city") or ""
	arr_city = result.get("arr_city") or ""

	if dep_date is None and flights:
		dep_date = flights[0]["dep_date"]

	captured_at = hourBucket()

	with db.cursor() as cur:
		# ---------- 1) 航班 + 价格：不依赖 route，无条件写入 ----------
		if flights:
			cur.executemany(SQL_UPSERT_FLIGHT, [_flight_row(f) for f in flights])
		if prices:
			cur.executemany(SQL_UPSERT_PRICE, [_price_row(p, captured_at) for p in prices])

		# ---------- 2) 航线：以解析结果为准，一次查询可能产出多条 ----------
		pairs = sorted({(f["dep_iata"], f["arr_iata"]) for f in flights})
		route_ids = {}

		for dep, arr in pairs:
			if dep not in KNOWN_AIRPORTS or arr not in KNOWN_AIRPORTS:
				logger.warning(f"未知机场 {dep}-{arr}，跳过航线登记（航班与价格已入库）")
				continue
			cur.execute(SQL_UPSERT_ROUTE, (dep, arr))
			cur.execute(SQL_SELECT_ROUTE_ID, (dep, arr))
			route_ids[(dep, arr)] = cur.fetchone()["id"]

		# 唯一航线时才有明确 route_id
		single_route_id = None
		if len(pairs) == 1 and pairs[0] in route_ids:
			single_route_id = route_ids[pairs[0]]

		# ---------- 3) 任务记录 ----------
		if record_task:
			cur.execute(SQL_INSERT_TASK, (
				single_route_id,
				dep_city,
				arr_city,
				dep_date,
				"search",
				result.get("raw_items") or len(flights),
				len(flights),
				len(prices),
				datetime.now(),
			))

	return len(flights), len(prices), route_ids


# ============================================================
# 解析入库编排
# ============================================================

def process_pending(db, mongo, limit: int = 200) -> dict:
	"""
	把 MongoDB 里 parse_status=waiting 的响应逐条解析入库。

	每条独立事务：一条失败不影响其他条，失败原因写回 Mongo。
	返回统计 {"total", "ok", "failed", "flights", "prices"}
	"""
	docs = list(mongo.col.find({"parse_status": "waiting"}).limit(limit))
	stat = {"total": len(docs), "ok": 0, "failed": 0, "flights": 0, "prices": 0}

	if not docs:
		return stat

	for doc in docs:
		sign = doc.get("sign")
		try:
			result, error = decode_raw(doc)
			if not result:
				mongo.markParsed(sign, "failed", error)
				stat["failed"] += 1
				continue

			# Mongo 里日期是 yyyymmdd，MySQL 要 yyyy-mm-dd
			dep_date = formatDateStr(result["dep_date"])

			n_flight, n_price, _ = save_result(
				db, result, dep_date=dep_date, record_task=False,
			)

			# 回写状态：data_is_none 以解析结果为准
			mongo.markParsed(sign, "success", data_is_none=(n_flight == 0))

			# 回填采集任务的统计
			db.execute(
				"update crawl_task set raw_items=%s, parsed_rows=%s, price_rows=%s "
				"where sign=%s",
				(result.get("raw_items") or n_flight, n_flight, n_price, sign),
			)

			stat["ok"] += 1
			stat["flights"] += n_flight
			stat["prices"] += n_price

			logger.info(
				f"[入库] {doc.get('dep_city')}->{doc.get('arr_city')} "
				f"{dep_date} | 航班 {n_flight} / 舱位 {n_price}"
			)

			# 数据刚入库，立刻检查有没有价格监控规则被触发
			check_monitors(db, doc.get("dep_city"), doc.get("arr_city"), dep_date)

		except Exception as e:
			logger.exception(f"[入库] {sign} 失败")
			stat["failed"] += 1
			try:
				mongo.markParsed(sign, "failed", str(e))
			except Exception:
				pass

	return stat


def parse_one(db, mongo, sign: str) -> dict:
	"""
	立即解析并入库**单条**原始响应。

	用于按需采集：用户点了查询、刚采完，不该再等 parse_pending 的 5 分钟轮询。

	返回：
	    {"flights": n, "prices": m, "error": None}   解析成功
	    {"flights": 0, "prices": 0, "error": "原因"}  解析失败

	注意：解析失败不会抛异常。数据已经采到了，解析失败交给 parse_pending 兜底，
	不能让一次解析异常把「采集成功」的结论推翻。
	"""
	doc = mongo.col.find_one({"sign": sign})
	if not doc:
		return {"flights": 0, "prices": 0, "error": "找不到原始响应"}

	result, error = decode_raw(doc)
	if not result:
		try:
			mongo.markParsed(sign, "failed", error)
		except Exception:
			pass
		return {"flights": 0, "prices": 0, "error": error or "解析失败"}

	dep_date = formatDateStr(result["dep_date"])

	try:
		# record_task=True：按需采集不进 Celery 队列，
		# 这条记录就是它的审计记录，check_daily 也能统计到
		n_flight, n_price, _ = save_result(
			db, result, dep_date=dep_date, record_task=True,
		)
	except Exception as e:
		logger.exception(f"[按需] {sign} 入库失败")
		try:
			mongo.markParsed(sign, "failed", str(e))
		except Exception:
			pass
		return {"flights": 0, "prices": 0, "error": f"入库失败：{e}"}

	try:
		mongo.markParsed(sign, "success", data_is_none=(n_flight == 0))
	except Exception:
		pass

	logger.info(
		f"[按需] {doc.get('dep_city')}->{doc.get('arr_city')} "
		f"{dep_date} | 航班 {n_flight} / 舱位 {n_price}"
	)

	# 按需采集同样要过一遍监控判定
	check_monitors(db, doc.get("dep_city"), doc.get("arr_city"), dep_date)

	return {"flights": n_flight, "prices": n_price, "error": None}
