# -*- coding: utf-8 -*-

from collections import defaultdict

from csair.data import AirPortCodeData, CityCodeData
from csair.data.city_initial import CITY_INITIAL, CITY_PINYIN

# 数据源里的占位键，不是真实机场
SKIP_AIRPORT_KEYS = {"NO_AIRPROT_CITY"}

# 无法判断首字母时的归组
FALLBACK_INITIAL = "#"


def _build_city_airports() -> dict:
	"""
	城市码 -> [机场码, ...]

	城市与机场是一对多：BJS -> [PEK, PKX]，成都 -> [CTU, TFU]。
	以机场表为准（AirPortCodeData 的 cityCode 字段），因为采集是按机场落库的。
	"""
	mapping = defaultdict(list)
	for iata, info in AirPortCodeData.items():
		if iata in SKIP_AIRPORT_KEYS:
			continue
		city = info.get("cityCode")
		if city:
			mapping[city].append(iata)
	for city in mapping:
		mapping[city].sort()
	return dict(mapping)


# 模块级缓存：数据是静态的，进程内只算一次
CITY_AIRPORTS = _build_city_airports()

# 有效机场码集合，用于判断某航线能否登记
KNOWN_AIRPORTS = set(AirPortCodeData) - SKIP_AIRPORT_KEYS


def resolve_airports(code: str) -> list:
	"""
	把入参解析成机场码列表。

	    城市码 BJS -> ['PEK', 'PKX']
	    机场码 PKX -> ['PKX']
	    无法识别   -> []

	入参既接受城市码也接受机场码，调用方不必预先判断类型。
	"""
	code = (code or "").strip().upper()
	if not code:
		return []

	if code in CITY_AIRPORTS:
		return list(CITY_AIRPORTS[code])
	if code in AirPortCodeData and code not in SKIP_AIRPORT_KEYS:
		return [code]
	return []


def city_name(code: str) -> str:
	"""取城市中文名；传入机场码时返回其所属城市名"""
	code = (code or "").strip().upper()
	city = CityCodeData.get(code)
	if city:
		return city.get("cityCnName") or code

	airport = AirPortCodeData.get(code)
	if airport:
		return airport.get("cityCnName") or code
	return code


def airport_name(code: str) -> str:
	"""取机场中文名"""
	airport = AirPortCodeData.get((code or "").strip().upper())
	return airport.get("airportCnName") if airport else ""


def city_of_airport(iata: str):
	"""机场码 -> 所属城市码。PKX -> BJS。找不到返回 None"""
	info = AirPortCodeData.get((iata or "").strip().upper())
	return info.get("cityCode") if info else None


def city_initial(code: str) -> str:
	"""
	城市首字母，用于 A-Z 分组。北京 -> B，乌鲁木齐 -> W。

	映射由 scripts/生成城市字母映照表.py 离线生成，
	运行时不需要 pypinyin 依赖。
	"""
	return CITY_INITIAL.get((code or "").strip().upper(), FALLBACK_INITIAL)


def city_pinyin(code: str) -> str:
	"""城市全拼小写，用于搜索框按拼音匹配。北京 -> beijing"""
	return CITY_PINYIN.get((code or "").strip().upper(), "")


def short_airport(name: str) -> str:
	"""机场全名太长，去掉「国际机场」「机场」后缀用于表格展示"""
	if not name:
		return "-"
	for suffix in ("国际机场", "机场"):
		if name.endswith(suffix):
			return name[: -len(suffix)]
	return name


def get_city(code: str):
	"""取城市原始字典，找不到返回 None"""
	return CityCodeData.get((code or "").strip().upper())


def iter_cities():
	"""遍历城市原始字典，供主数据同步使用"""
	return CityCodeData.items()


def iter_airports():
	"""遍历机场原始字典（已排除占位键）"""
	for code, info in AirPortCodeData.items():
		if code in SKIP_AIRPORT_KEYS:
			continue
		yield code, info
