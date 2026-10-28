# -*- coding: utf-8 -*-

import time
from datetime import datetime, time as dtime, timedelta

from csair.assets import (
	CITY_AIRPORTS,
	city_initial,
	city_name,
	city_of_airport,
	city_pinyin,
	iter_cities,
	resolve_airports,
	short_airport,
)
from csair.database import MySQLDB
from csair.exceptions import InvalidCityError, InvalidParamError, NotFoundError
from csair.settings import HOT_CITIES


# ============================================================
# SQL
# ============================================================

SEARCH_SQL = """
select f.flight_no,
       f.dep_date,
       f.dep_iata,
       f.arr_iata,
       f.dep_time,
       f.arr_time,
       f.arr_date,
       f.duration_min,
       f.aircraft,
       f.dep_term,
       f.arr_term,
       f.stop_count,
       f.is_code_share,
       f.marketing_flight_no,
       ad.name_cn as dep_airport_name,
       aa.name_cn as arr_airport_name,
       lp.lowest_total,
       lp.cabin_cnt
from flight f
         join airport ad on ad.iata = f.dep_iata
         join airport aa on aa.iata = f.arr_iata
         left join (select p.flight_no,
                           p.dep_date,
                           min(p.price_total) as lowest_total,
                           count(*)           as cabin_cnt
                    from price_snapshot p
                    where p.dep_date = %s
                      and (%s = 'ALL' or p.base_cabin = %s)
                    group by p.flight_no, p.dep_date) lp
                   on lp.flight_no = f.flight_no and lp.dep_date = f.dep_date
where f.dep_iata in ({deps})
  and f.arr_iata in ({arrs})
  and f.dep_date = %s
"""

TREND_BY_DATE_SQL = """
select d.dep_date,
       min(d.low)        as lowest,
       round(avg(d.low)) as avg_price,
       max(d.low)        as highest,
       count(*)          as flights
from (select p.dep_date,
             p.flight_no,
             min(p.price_total) as low
      from price_snapshot p
               join flight f on f.flight_no = p.flight_no and f.dep_date = p.dep_date
      where f.dep_iata in ({deps})
        and f.arr_iata in ({arrs})
        and p.base_cabin = %s
        and p.dep_date between %s and %s
      group by p.dep_date, p.flight_no) d
group by d.dep_date
order by d.dep_date
"""

TREND_BY_TIME_SQL = """
select d.captured_at,
       min(d.low)        as lowest,
       round(avg(d.low)) as avg_price,
       sum(d.cnt)        as rows_cnt
from (select p.captured_at,
             p.flight_no,
             min(p.price_total) as low,
             count(*)           as cnt
      from price_snapshot p
               join flight f on f.flight_no = p.flight_no and f.dep_date = p.dep_date
      where f.dep_iata in ({deps})
        and f.arr_iata in ({arrs})
        and p.base_cabin = %s
        and p.dep_date = %s
      group by p.captured_at, p.flight_no) d
group by d.captured_at
order by d.captured_at
"""

ROUTES_SQL = """
select r.dep_iata,
       r.arr_iata,
       ad.name_cn as dep_airport_name,
       aa.name_cn as arr_airport_name,
       r.status,
       count(distinct f.dep_date) as days,
       count(f.flight_no)         as flights
from route r
         join airport ad on ad.iata = r.dep_iata
         join airport aa on aa.iata = r.arr_iata
         left join flight f on f.dep_iata = r.dep_iata and f.arr_iata = r.arr_iata
group by r.dep_iata, r.arr_iata, ad.name_cn, aa.name_cn, r.status
having flights > 0
order by flights desc
"""

# 单个航班的航段信息
FLIGHT_DETAIL_SQL = """
select f.flight_no,
       f.dep_date,
       f.arr_date,
       f.dep_iata,
       f.arr_iata,
       f.dep_time,
       f.arr_time,
       f.duration_min,
       f.aircraft,
       f.aircraft_name,
       f.dep_term,
       f.arr_term,
       f.stop_count,
       f.is_code_share,
       f.marketing_flight_no,
       ad.name_cn as dep_airport_name,
       aa.name_cn as arr_airport_name
from flight f
         join airport ad on ad.iata = f.dep_iata
         join airport aa on aa.iata = f.arr_iata
where f.flight_no = %s
  and f.dep_date = %s
"""

# 该航班在「最近一次采集」里的全部舱位产品
# 只取最新快照：price_snapshot 每采集一轮会产生一批，历史快照不进详情
CABINS_SQL = """
select p.cabin_code,
       p.cabin_class,
       p.base_cabin,
       p.price,
       p.tax,
       p.price_total,
       p.seats_left,
       p.seats_is_lower_bound,
       p.fare_basis,
       p.refund_change_type,
       p.captured_at
from price_snapshot p
where p.flight_no = %s
  and p.dep_date = %s
  and p.captured_at = (select max(p2.captured_at)
                       from price_snapshot p2
                       where p2.flight_no = p.flight_no
                         and p2.dep_date = p.dep_date)
order by p.base_cabin desc, p.price_total asc
"""


# ============================================================
# 内部工具
# ============================================================

def _ph(items) -> str:
	"""生成 %s,%s,%s 形式的占位符串"""
	return ",".join(["%s"] * len(items))


def _fmt_time(value) -> str:
	"""
	MySQL 的 TIME 列经 pymysql 读出来是 datetime.timedelta
	（因为 TIME 允许负数、允许超过 24 小时），不能直接 strftime。
	"""
	if value is None:
		return None
	if isinstance(value, timedelta):
		total = int(value.total_seconds())
		if total < 0:
			return None
		hh, rem = divmod(total, 3600)
		return f"{hh:02d}:{rem // 60:02d}"
	if isinstance(value, dtime):
		return value.strftime("%H:%M")
	return str(value)[:5]


def _num(value):
	"""Decimal -> float，None 保持 None。用于 JSON 序列化"""
	return float(value) if value is not None else None


def _norm_date(value):
	"""接受 date / '2026-10-28' / '20261028'，统一成 date 对象"""
	if isinstance(value, datetime):
		return value.date()
	if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day"):
		return value

	text = str(value).strip()
	for fmt in ("%Y-%m-%d", "%Y%m%d"):
		try:
			return datetime.strptime(text, fmt).date()
		except ValueError:
			continue
	raise InvalidParamError(f"日期格式不对：{text}（应为 2026-10-28 或 20261028）")


def _resolve_side(code: str, label: str) -> dict:
	"""把入参解析成 {'code','name','airports'}，识别不了抛 InvalidCityError"""
	airports = resolve_airports(code)
	if not airports:
		raise InvalidCityError(f"无法识别{label}：{code}")
	return {
		"code": str(code).strip().upper(),
		"name": city_name(code),
		"airports": airports,
	}


# ============================================================
# 航班查询
# ============================================================

def search_flights(dep: str, arr: str, dep_date,
                   cabin: str = "Y", direct: bool = False,
                   sort: str = "time", limit: int = 0) -> dict:
	"""
	查某条航线某天的航班。

	:param dep: 出发城市码或机场码（CSX / BJS / PKX）
	:param arr: 到达城市码或机场码
	:param dep_date: 航班日期
	:param cabin: Y = 经济舱系列 / J = 公务舱系列 / ALL = 全部
	:param direct: 只看直飞
	:param sort: price / time / duration
	:param limit: 最多返回几条，0 表示不限
	:return: {"dep","arr","date","cabin","direct_only","count","flights","summary"}
	:raises InvalidCityError: 城市码无法识别
	"""
	dep_side = _resolve_side(dep, "出发地")
	arr_side = _resolve_side(arr, "到达地")
	day = _norm_date(dep_date)

	sql = SEARCH_SQL.format(
		deps=_ph(dep_side["airports"]),
		arrs=_ph(arr_side["airports"]),
	)
	if direct:
		sql += " and f.stop_count = 0"
	# 指定舱位类别时，只保留真正有该舱位的航班
	if cabin != "ALL":
		sql += " and lp.lowest_total is not null"

	order_map = {
		"price": "lp.lowest_total is null, lp.lowest_total asc, f.dep_time asc",
		"time": "f.dep_time asc",
		"duration": "f.duration_min asc, f.dep_time asc",
	}
	sql += " order by " + order_map.get(sort, order_map["time"])
	if limit:
		sql += " limit %s"

	params = ([day, cabin, cabin]
	          + dep_side["airports"] + arr_side["airports"] + [day])
	if limit:
		params.append(limit)

	db = MySQLDB()
	rows = db.query(sql, tuple(params))

	flights = []
	for r in rows:
		dep_ap = short_airport(r["dep_airport_name"])
		if (r["dep_term"] or "").strip():
			dep_ap += " " + r["dep_term"].strip()

		arr_ap = short_airport(r["arr_airport_name"])
		if (r["arr_term"] or "").strip():
			arr_ap += " " + r["arr_term"].strip()

		flights.append({
			"flight_no": r["flight_no"],
			"dep_date": r["dep_date"].isoformat(),
			"dep_iata": r["dep_iata"],
			"arr_iata": r["arr_iata"],
			"dep_airport": dep_ap,
			"arr_airport": arr_ap,
			"dep_term": (r["dep_term"] or "").strip() or None,
			"arr_term": (r["arr_term"] or "").strip() or None,
			"dep_time": _fmt_time(r["dep_time"]),
			"arr_time": _fmt_time(r["arr_time"]),
			"arr_next_day": bool(r["arr_date"] and r["arr_date"] != r["dep_date"]),
			"duration_min": r["duration_min"],
			"aircraft": r["aircraft"],
			"stop_count": r["stop_count"],
			"is_code_share": bool(r["is_code_share"]),
			"marketing_flight_no": r["marketing_flight_no"],
			"lowest_total": _num(r["lowest_total"]),
			"cabin_count": int(r["cabin_cnt"] or 0),
		})

	prices = [f["lowest_total"] for f in flights if f["lowest_total"] is not None]

	return {
		"dep": dep_side,
		"arr": arr_side,
		"date": day.isoformat(),
		"cabin": cabin,
		"direct_only": bool(direct),
		"count": len(flights),
		"flights": flights,
		"summary": {
			"lowest": min(prices) if prices else None,
			"highest": max(prices) if prices else None,
			"avg": round(sum(prices) / len(prices), 1) if prices else None,
		},
	}


# ============================================================
# 价格走势
# ============================================================

def price_trend(dep: str, arr: str, start_date=None, days: int = 7,
                by: str = "date", cabin: str = "Y") -> dict:
	"""
	价格走势。

	by="date"：
	    未来 N 天各航班日期的最低价 —— 回答「哪天的票便宜」
	by="time"：
	    同一天在不同采集时刻的价格 —— 回答「价格涨了还是跌了」

	:return: {"dep","arr","by","cabin","rows","summary"}
	"""
	dep_side = _resolve_side(dep, "出发地")
	arr_side = _resolve_side(arr, "到达地")
	start = _norm_date(start_date or datetime.now().date())

	db = MySQLDB()
	deps_ph, arrs_ph = dep_side["airports"], arr_side["airports"]

	if by == "date":
		end = start + timedelta(days=max(1, days) - 1)
		rows = db.query(
			TREND_BY_DATE_SQL.format(deps=_ph(deps_ph), arrs=_ph(arrs_ph)),
			tuple(deps_ph + arrs_ph + [cabin, start, end]),
		)

		data = [{
			"dep_date": r["dep_date"].isoformat(),
			"lowest": _num(r["lowest"]),
			"avg": _num(r["avg_price"]),
			"highest": _num(r["highest"]),
			"flights": int(r["flights"] or 0),
		} for r in rows]

		summary = {}
		if data:
			best = min(data, key=lambda x: x["lowest"])
			summary = {"cheapest_date": best["dep_date"], "cheapest_price": best["lowest"]}

	else:
		rows = db.query(
			TREND_BY_TIME_SQL.format(deps=_ph(deps_ph), arrs=_ph(arrs_ph)),
			tuple(deps_ph + arrs_ph + [cabin, start]),
		)

		data = [{
			"captured_at": r["captured_at"].strftime("%Y-%m-%d %H:%M"),
			"lowest": _num(r["lowest"]),
			"avg": _num(r["avg_price"]),
			"rows_cnt": int(r["rows_cnt"] or 0),
		} for r in rows]

		summary = {}
		if len(data) >= 2:
			first, last = data[0]["lowest"], data[-1]["lowest"]
			diff = round(last - first, 1)
			summary = {
				"first": first,
				"last": last,
				"change": diff,
				"direction": "up" if diff > 0 else ("down" if diff < 0 else "flat"),
			}

	return {
		"dep": dep_side,
		"arr": arr_side,
		"by": by,
		"cabin": cabin,
		"rows": data,
		"summary": summary,
	}


# ============================================================
# 航线列表
# ============================================================

def list_routes() -> list:
	"""列出已登记且有航班数据的航线，按航班数倒序"""
	db = MySQLDB()
	rows = db.query(ROUTES_SQL)
	return [{
		"dep_iata": r["dep_iata"],
		"arr_iata": r["arr_iata"],
		"dep_airport": short_airport(r["dep_airport_name"]),
		"arr_airport": short_airport(r["arr_airport_name"]),
		"status": r["status"],
		"days": int(r["days"] or 0),
		"flights": int(r["flights"] or 0),
	} for r in rows]


# 有航班数据的机场码（只扫 flight 表的两列，比 list_routes 的聚合快很多）
DATA_AIRPORTS_SQL = """
select distinct dep_iata as iata from flight
union
select distinct arr_iata from flight
"""

# 城市列表是静态数据，只有 has_data 会随采集变化，缓存几分钟足够
_CITIES_CACHE = {"data": None, "at": 0.0}
CITIES_TTL_SECONDS = 300


def _build_cities() -> list:
	"""构建完整的城市列表（内部使用，带缓存）"""
	# ---------- 有数据的城市 ----------
	data_codes = set()
	for row in MySQLDB().query(DATA_AIRPORTS_SQL):
		code = city_of_airport(row["iata"])
		if code:
			data_codes.add(code)

	hot = set(HOT_CITIES)

	# ---------- 全部城市 ----------
	items = []
	for code, city in iter_cities():
		airports = CITY_AIRPORTS.get(code) or []
		if not airports:
			continue   # 没有机场的城市无法查询

		items.append({
			"code": code,
			"name": city.get("cityCnName") or code,
			"airports": list(airports),
			"initial": city_initial(code),
			"pinyin": city_pinyin(code),
			"is_hot": code in hot,
			"has_data": code in data_codes,
		})

	# A-Z 分组顺序 + 组内按拼音
	items.sort(key=lambda x: (x["initial"], x["pinyin"], x["code"]))
	return items


def list_cities(include_all: bool = True) -> list:
	"""
	城市列表，给前端选择器用。

	返回全部城市（约 680 个），带分组所需的信息：
	    initial   A-Z 首字母，用于分组
	    pinyin    全拼，用于搜索框按拼音匹配
	    is_hot    是否在采集范围内的热门城市
	    has_data  是否已经采到过航班数据

	航线表存的是机场码（CSX->PKX），前端要的是城市码（CSX->BJS），
	所以先按机场反查城市，再和城市主数据合并。

	:param include_all: False 时只返回 has_data 的城市
	"""
	now = time.time()
	if (_CITIES_CACHE["data"] is None
			or now - _CITIES_CACHE["at"] > CITIES_TTL_SECONDS):
		_CITIES_CACHE["data"] = _build_cities()
		_CITIES_CACHE["at"] = now

	items = _CITIES_CACHE["data"]
	return list(items) if include_all else [x for x in items if x["has_data"]]


# ============================================================
# 舱位明细（F-002）
# ============================================================

BASE_CABIN_LABEL = {
	"Y": "经济舱",
	"J": "公务舱",
	"F": "头等舱",
}


def flight_cabins(flight_no: str, dep_date) -> dict:
	"""
	查询某个航班的全部舱位产品。

	:param flight_no: 航班号，如 CZ3123
	:param dep_date: 航班日期，2026-10-28 或 20261028
	:return: {"flight": {...}, "captured_at", "cabin_count", "cabins": [...]}
	:raises InvalidParamError: 航班号格式不对 / 日期格式不对
	:raises NotFoundError: 该航班当天没有数据

	为什么只取最新快照：
	    price_snapshot 每采集一轮写入一批，同一个航班会有多轮历史。
	    详情页要展示「现在的价格」，历史走势由 /trend 负责。
	"""
	flight_no = (flight_no or "").strip().upper()
	if not flight_no:
		raise InvalidParamError("航班号不能为空")

	day = _norm_date(dep_date)
	db = MySQLDB()

	meta = db.query_one(FLIGHT_DETAIL_SQL, (flight_no, day))
	if not meta:
		raise NotFoundError(f"没有找到 {flight_no} 在 {day} 的记录")

	dep_ap = short_airport(meta["dep_airport_name"])
	if (meta["dep_term"] or "").strip():
		dep_ap += " " + meta["dep_term"].strip()

	arr_ap = short_airport(meta["arr_airport_name"])
	if (meta["arr_term"] or "").strip():
		arr_ap += " " + meta["arr_term"].strip()

	rows = db.query(CABINS_SQL, (flight_no, day))

	cabins = [{
		"cabin_code": r["cabin_code"],
		"cabin_class": r["cabin_class"],
		"base_cabin": r["base_cabin"] or "",
		"base_cabin_label": BASE_CABIN_LABEL.get(r["base_cabin"] or "", "其他"),
		"price": _num(r["price"]),
		"tax": _num(r["tax"]),
		"price_total": _num(r["price_total"]),
		"seats_left": r["seats_left"],
		"seats_is_lower_bound": bool(r["seats_is_lower_bound"]),
		"fare_basis": r["fare_basis"],
		"refund_change_type": r["refund_change_type"],
	} for r in rows]

	captured = rows[0]["captured_at"] if rows else None

	return {
		"flight": {
			"flight_no": meta["flight_no"],
			"dep_date": meta["dep_date"].isoformat(),
			"arr_date": meta["arr_date"].isoformat() if meta["arr_date"] else None,
			"dep_iata": meta["dep_iata"],
			"arr_iata": meta["arr_iata"],
			"dep_airport": dep_ap,
			"arr_airport": arr_ap,
			"dep_time": _fmt_time(meta["dep_time"]),
			"arr_time": _fmt_time(meta["arr_time"]),
			"arr_next_day": bool(
				meta["arr_date"] and meta["arr_date"] != meta["dep_date"]
			),
			"duration_min": meta["duration_min"],
			"aircraft": meta["aircraft"],
			"aircraft_name": meta["aircraft_name"],
			"stop_count": meta["stop_count"],
			"is_code_share": bool(meta["is_code_share"]),
			"marketing_flight_no": meta["marketing_flight_no"],
		},
		"captured_at": captured.strftime("%Y-%m-%d %H:%M") if captured else None,
		"cabin_count": len(cabins),
		"cabins": cabins,
	}


# ============================================================
# 单航线最低价（价格监控用）
# ============================================================

LOWEST_SQL = """
select f.flight_no,
       f.dep_time,
       f.arr_time,
       p.cabin_code,
       p.cabin_class,
       p.price_total,
       p.captured_at
from price_snapshot p
         join flight f on f.flight_no = p.flight_no and f.dep_date = p.dep_date
where f.dep_iata in ({deps})
  and f.arr_iata in ({arrs})
  and p.dep_date = %s
  and (%s = 'ALL' or p.base_cabin = %s)
  and p.captured_at = (select max(p2.captured_at)
                       from price_snapshot p2
                       where p2.flight_no = p.flight_no
                         and p2.dep_date = p.dep_date)
order by p.price_total asc
limit 1
"""


def lowest_offer(db, dep: str, arr: str, dep_date, cabin_scope: str = "Y"):
	"""
	该航线日期当前的最低含税价，带航班与舱位信息。

	和 search_flights 的区别：
	    search_flights 返回全部航班，这个只返回最便宜的那一条，
	    而且**只读数据库、不触发按需采集** —— 价格监控要频繁轮询，
	    不能每次都去打接口。

	没有数据返回 None。
	"""
	deps = resolve_airports(dep)
	arrs = resolve_airports(arr)
	if not deps or not arrs:
		return None

	day = _norm_date(dep_date)
	row = db.query_one(
		LOWEST_SQL.format(deps=_ph(deps), arrs=_ph(arrs)),
		tuple(deps + arrs + [day, cabin_scope, cabin_scope]),
	)
	if not row:
		return None

	return {
		"flight_no": row["flight_no"],
		"dep_time": _fmt_time(row["dep_time"]),
		"arr_time": _fmt_time(row["arr_time"]),
		"cabin_code": row["cabin_code"],
		"cabin_class": row["cabin_class"] or "",
		"price_total": _num(row["price_total"]),
		"captured_at": (row["captured_at"].strftime("%Y-%m-%d %H:%M")
		                if row["captured_at"] else None),
	}
