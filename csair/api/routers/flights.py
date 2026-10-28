# -*- coding: utf-8 -*-
from datetime import date as _date

from fastapi import APIRouter, Query
from loguru import logger

from csair.api.schemas import (
	CabinResponse,
	EmptyInfo,
	LowestResponse,
	SearchResponse,
	TrendResponse,
)
from csair.database import MySQLDB
from csair.ondemand import collect_on_demand
from csair.queries import flight_cabins, lowest_offer, price_trend, search_flights

router = APIRouter(prefix="/api/flights", tags=["flights"])

# 空结果给用户看的中文说明
EMPTY_MESSAGES = {
	"no_flight": "当天确实没有可售航班，换个日期试试",
	"no_data": "这条航线近期多次查询都没有结果，2 小时内不再自动采集",
	"collecting": "正在采集这条航线，稍后再看",
	"throttled": "当前查询较多，请稍后再试",
	"upstream_error": "采集失败，请稍后重试",
	"api_error": "这条航线暂时查询不到（接口返回业务限制）",
	"invalid_city": "城市码无效",
	"past_date": "该日期已过，航班已不可预订",
	"disabled": "按需采集未开启",
}

# 这些原因过一会儿重试有意义
RETRYABLE = {"collecting", "throttled", "upstream_error"}


def _empty(reason: str, retry_after: int | None = None,
           detail: str = "") -> EmptyInfo:
	message = EMPTY_MESSAGES.get(reason, "没有查到航班")
	# 业务错误码带上接口原文，比如「B0952 日期太远」
	if reason == "api_error" and detail:
		message = f"这条航线暂时查不到：{detail}"

	return EmptyInfo(
		reason=reason,
		message=message,
		retryable=reason in RETRYABLE,
		retry_after=retry_after if reason in RETRYABLE else None,
	)


@router.get(
	"/search",
	response_model=SearchResponse,
	response_model_exclude_none=True,
	summary="查询某天某航线的航班",
)
def search(
		dep: str = Query(description="出发城市码或机场码，如 CSX / BJS / PKX"),
		arr: str = Query(description="到达城市码或机场码"),
		date: str = Query(description="航班日期，2026-10-28 或 20261028"),
		cabin: str = Query(default="Y", pattern="^(Y|J|ALL)$",
		                   description="舱位范围：Y 经济舱 / J 公务舱 / ALL 全部"),
		direct: bool = Query(default=False, description="只看直飞"),
		sort: str = Query(default="time", pattern="^(price|time|duration)$",
		                  description="排序方式"),
		limit: int = Query(default=0, ge=0, le=200,
		                   description="最多返回几条，0 表示不限"),
):
	"""
	查询结果里的 `lowest_total` 是**含税价**（price_total），不是票面价。

	城市码会自动展开成该城市的所有机场：
	`BJS` -> `PEK` + `PKX`，所以一次请求能同时覆盖首都和大兴。

	**查不到时会自动采一次**（按需采集）：
	    同步执行，约 4~5 秒，采到数据后立即解析入库并重新查询。
	    失败或被限流时返回 `empty` 说明原因。
	"""
	result = search_flights(
		dep=dep, arr=arr, dep_date=date,
		cabin=cabin, direct=direct, sort=sort, limit=limit,
	)
	if result.get("flights"):
		logger.info(f"航线 <日期：{date} 起始：{dep} -> 站点：{arr}> 数据存在，返回.")
		return result

	# ---------- 空结果：先判断值不值得采 ----------
	if result["date"] < _date.today().isoformat():
		result["empty"] = _empty("past_date")
		logger.info(f"航线 <日期：{date} 起始：{dep} -> 站点：{arr}> 日期不符合条件.")
		return result

	# 按需采一次（内部自带并发锁 / 负缓存 / 全局限速）
	collected = collect_on_demand(dep, arr, result["date"])

	if collected.get("ok"):
		# 采到了，重新查一遍
		refreshed = search_flights(
			dep=dep, arr=arr, dep_date=date,
			cabin=cabin, direct=direct, sort=sort, limit=limit,
		)
		if refreshed.get("flights"):
			logger.success(f"航线 <日期：{date} 起始：{dep} -> 站点：{arr}> 数据采集成功.")

			return refreshed
		result = refreshed

	reason = collected.get("reason", "no_flight")
	logger.info(f"航线 <日期：{date} 起始：{dep} -> 站点：{arr}> {reason}.")
	result["empty"] = _empty(reason, collected.get("retry_after"),
	                         collected.get("detail", ""))
	return result


@router.get(
	"/trend",
	response_model=TrendResponse,
	response_model_exclude_none=True,
	summary="价格走势",
)
def trend(
		dep: str = Query(description="出发城市码或机场码"),
		arr: str = Query(description="到达城市码或机场码"),
		start: str | None = Query(
			default=None, description="起始航班日期，默认今天"),
		days: int = Query(default=7, ge=1, le=30,
		                  description="by=date 时看几天"),
		by: str = Query(default="date", pattern="^(date|time)$",
		                description="date=未来各天哪天便宜 / time=同一天的价格涨跌"),
		cabin: str = Query(default="Y", pattern="^(Y|J)$",
		                   description="舱位类别"),
):
	"""
	`by=date`：回答「哪天的票便宜」，rows 里每天一行。

	`by=time`：回答「价格涨了还是跌了」，需要同一航班日期被多次采集才有数据。
	此时 `start` 表示要观察的**航班日期**，不是采集日期。
	"""
	return price_trend(
		dep=dep, arr=arr, start_date=start,
		days=days, by=by, cabin=cabin,
	)


@router.get(
	"/lowest",
	response_model=LowestResponse,
	response_model_exclude_none=True,
	summary="单航线最低价（只读，不触发采集）",
)
def lowest(
		dep: str = Query(description="出发城市码或机场码"),
		arr: str = Query(description="到达城市码或机场码"),
		date: str = Query(description="航班日期，2026-10-30 或 20261030"),
		cabin: str = Query(default="Y", pattern="^(Y|J|ALL)$",
		                   description="舱位范围"),
):
	"""
	价格监控列表用它刷新「当前价」。

	**刻意做成只读**：监控规则存在浏览器本地，这个接口只回答
	「这条航线现在多少钱」这个公开信息，所以：
	    - 不接收监控 id，拿不到别人的规则
	    - **不触发按需采集**，否则轮询会把采集配额烧光
	"""
	db = MySQLDB()
	offer = lowest_offer(db, dep, arr, date, cabin)

	return LowestResponse(
		dep=dep.upper(),
		arr=arr.upper(),
		date=date,
		cabin=cabin,
		lowest=offer["price_total"] if offer else None,
		flight_no=offer["flight_no"] if offer else None,
		dep_time=offer["dep_time"] if offer else None,
		arr_time=offer["arr_time"] if offer else None,
		cabin_code=offer["cabin_code"] if offer else None,
		cabin_class=offer["cabin_class"] if offer else None,
		captured_at=offer["captured_at"] if offer else None,
	)


@router.get(
	"/{flight_no}/cabins",
	response_model=CabinResponse,
	response_model_exclude_none=True,
	summary="查询某个航班的全部舱位",
)
def cabins(
		flight_no: str,
		dep_date: str = Query(description="航班日期，2026-10-28 或 20261028",
		                      alias="dep_date"),
):
	"""
	返回该航班**最近一次采集**的全部舱位产品。

	同一个舱位代码可能有多个产品（不同 `fare_basis`），例如：
	`V` 舱既有「新客特惠」也有「特价经济舱」，价格不同，两条都会返回。

	按「舱位大类（经济舱优先）→ 含税价升序」排列。
	"""
	return flight_cabins(flight_no, dep_date)
