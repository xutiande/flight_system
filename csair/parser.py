# -*- coding: utf-8 -*-

import re
from datetime import datetime, timedelta

# cabinsY = 经济舱系列；cabinsFJ = 公务舱(F)+公务舱(J) 系列
CABIN_ARRAYS = ("cabinsY", "cabinsFJ")


# ============================================================
# 字段级转换
# ============================================================

def _str_or_none(v):
	"""null / 空串 / 纯空格 统一返回 None，避免 '' 与 None 混用造成下游判断歧义"""
	if v is None:
		return None
	s = str(v).strip()
	return s or None


def _parse_hhmm(s):
	"""
	源格式：'0800'（4 位 HHMM，无冒号）
	目标：'08:00:00'（MySQL TIME）
	"""
	s = _str_or_none(s)
	if not s or len(s) != 4 or not s.isdigit():
		return None
	return f"{s[:2]}:{s[2:]}:00"


def _parse_duration(s):
	"""
	源格式：'2h25m'
	目标：145（分钟整数，便于 SQL 排序 / 均值 / 区间筛选）
	"""
	m = re.match(r"(\d+)h(\d+)m", _str_or_none(s) or "")
	return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _parse_seats(s):
	"""
	余票解析。info 有两种形态：
		'>9' -> (9, True)     下限值：至少 9 张，9 是显示上限不是真实库存
		'4'  -> (4, False)    精确值：剩 4 张
		''   -> (None, False) 未知
	返回 (余票数, 是否为下限)
	"""
	s = _str_or_none(s)
	if not s:
		return None, False
	if s.startswith(">"):
		try:
			return int(s.lstrip(">")), True
		except ValueError:
			return None, False
	try:
		return int(s), False
	except ValueError:
		return None, False


def _is_true(v):
	"""codeShare 是字符串 'TRUE'/'FALSE'，不是 JSON 布尔"""
	return (_str_or_none(v) or "").upper() == "TRUE"


def _calc_arr_date(dep_date, dep_time, arr_time):
	"""
	推算到达日期。跨天航班：dep 21:50 + 150min = 次日 00:20。
	若直接拼 dep_date + arr_time，会得到「到达早于出发」的错误数据。
	"""
	if not (dep_date and dep_time and arr_time):
		return None
	try:
		dep_dt = datetime.strptime(f"{dep_date} {dep_time}", "%Y-%m-%d %H:%M:%S")
	except ValueError:
		return None
	hh, mm = int(arr_time[:2]), int(arr_time[3:5])
	arr_dt = dep_dt.replace(hour=hh, minute=mm, second=0)
	if arr_dt <= dep_dt:
		arr_dt += timedelta(days=1)
	return arr_dt.date().isoformat()


# ============================================================
# 主解析
# ============================================================

def count_raw_flights(raw: dict) -> int:
	"""
	从原始响应里数航班条目数，与解析是否成功无关。

	这是「解析失败率」的正确分母：
	    响应里有 10 条，只解析出 8 条 -> 失败了 2 条
	如果用 len(flights) 当分母，失败永远统计不到。
	"""
	root = raw.get("flight") or {}
	return sum(len(seg.get("dateFlight") or []) for seg in root.get("segment") or [])


def parseSelectFight(raw: dict) -> dict:
	"""
	解析航班查询响应。

	返回：
	    flights      航班级结构化列表
	    prices       舱位价格列表（一个航班多个舱位产品）
	    warnings     跳过的条目说明
	    segment_count 航段数
	    item_count   解析出的航班数
	    raw_items    原始响应里的航班条目数
	"""
	root = raw.get("flight") or {}
	segments = root.get("segment") or []

	flights, prices, warnings = [], [], []

	for seg in segments:
		# segment 级字段：整段共享的航线与日期（多段时为中转/往返）
		seg_date = seg.get("date")
		for f in seg.get("dateFlight") or []:

			# ---------- 航班标识 ----------
			flight_no = (_str_or_none(f.get("flightNo")) or "").upper()
			if not flight_no:
				warnings.append("跳过：缺少 flightNo")
				continue

			# ---------- 时间 ----------
			dep_date = _str_or_none(f.get("depDate")) or seg_date
			dep_time = _parse_hhmm(f.get("depTime"))
			arr_time = _parse_hhmm(f.get("arrTime"))
			arr_date = _calc_arr_date(dep_date, dep_time, arr_time)

			# ---------- 航段 ----------
			dep = _str_or_none(f.get("depPort")) or _str_or_none(seg.get("depCity"))
			arr = _str_or_none(f.get("arrPort")) or _str_or_none(seg.get("arrCity"))

			# ---------- 代码共享 ----------
			is_code_share = _is_true(f.get("codeShare"))
			marketing_no = _str_or_none(f.get("codeShareInfo"))

			flights.append({
				# 销售航班号（南航自己的号，代码共享时也用它对外展示）
				"flight_no": flight_no,
				# 出发日期 yyyy-MM-dd
				"dep_date": dep_date,
				# 推算的到达日期；跨天航班用它，不能用 dep_date 代替
				"arr_date": arr_date,
				# 出发/到达机场 IATA 码（注意是机场码不是城市码）
				"dep_iata": dep,
				"arr_iata": arr,
				# 出发/到达时间 HH:MM:SS
				"dep_time": dep_time,
				"arr_time": arr_time,
				# 机型 IATA 代码
				"aircraft": _str_or_none(f.get("plane")),
				# 机型中文名；代码共享航班常为空
				"aircraft_name": _str_or_none(f.get("planeZhName")),
				# 航站楼；部分机场不返回，必须允许 NULL
				"dep_term": _str_or_none(f.get("depTerm")),
				"arr_term": _str_or_none(f.get("arrTerm")),
				# 飞行时长（分钟）
				"duration_min": _parse_duration(f.get("flyTime")),
				# 经停次数（0 = 直飞）
				"stop_count": int(_str_or_none(f.get("stopNumber")) or 0),
				# 是否代码共享：true 表示实际承运人不是南航
				"is_code_share": is_code_share,
				# 实际承运方航班号；查航班动态要用这个号
				"marketing_flight_no": marketing_no,
				# 是否提供餐食
				"meal": _str_or_none(f.get("meal")),
				# 最低票面价（不含税！展示给用户必须加税）
				"lowest_price": f.get("lowPrice"),
			})

			# ---------- 舱位价格 ----------
			for key in CABIN_ARRAYS:
				for c in f.get(key) or []:
					# 舱位代码（字母）
					code = _str_or_none(c.get("name"))
					# 票面价（不含税）
					face_price = c.get("adultPrice")
					if not code or not face_price:
						warnings.append(f"{flight_no} 舱位{code or '?'}无价格，跳过")
						continue

					# 税费合计 = 机场建设费 + 燃油附加费
					tax = c.get("adultTaxTotal") or 0
					# 含税总价；源字段通常已给出，缺失时自行相加兜底
					total = c.get("adultFareTotal") or (face_price + tax)

					seats, is_lower = _parse_seats(c.get("info"))

					prices.append({
						"flight_no": flight_no,
						"dep_date": dep_date,
						# 舱位代码，如 L / V / Z / J
						"cabin_code": code,
						# 产品品牌名（"新客特惠"/"特价经济舱"等），不是舱位等级
						"cabin_class": _str_or_none(c.get("label")),
						# 舱位大类：Y=经济舱系列，J=公务舱系列 —— 分类统计用这个
						"base_cabin": _str_or_none(c.get("baseCabin")),
						# 票面价（不含税）
						"price": face_price,
						# 税费
						"tax": tax,
						# 含税总价（用户实付）—— 展示与比价都应使用它
						"price_total": total,
						# 余票数；is_lower_bound=True 时表示"至少"而非精确值
						"seats_left": seats,
						"seats_is_lower_bound": is_lower,
						# 运价基础代码 —— 同一舱位代码下区分不同产品的关键字段
						"fare_basis": _str_or_none(c.get("adultFareBasis")),
						# 退改费用档位（"退改费用高"/"退改费用低"）
						"refund_change_type": _str_or_none(c.get("changePriceType")),
					})

	return {
		"flights": flights,
		"prices": prices,
		"warnings": warnings,
		"segment_count": len(segments),
		"item_count": len(flights),
		"raw_items": count_raw_flights(raw),
	}
