# -*- coding: utf-8 -*-
from pydantic import BaseModel, Field


class Side(BaseModel):
	"""航线的一端：入参的城市码，以及它展开出的机场列表"""
	code: str = Field(description="入参原文，如 BJS")
	name: str = Field(description="城市中文名，如 北京")
	airports: list[str] = Field(description="展开后的机场码，如 ['PEK','PKX']")


class Flight(BaseModel):
	"""单个航班"""
	flight_no: str = Field(description="航班号，如 CZ3123")
	dep_date: str = Field(description="航班日期 yyyy-MM-dd")
	dep_iata: str = Field(description="出发机场三字码")
	arr_iata: str = Field(description="到达机场三字码")
	dep_airport: str = Field(description="出发机场简称（含航站楼），如 长沙黄花 T2")
	arr_airport: str = Field(description="到达机场简称（含航站楼）")
	dep_term: str | None = Field(default=None, description="出发航站楼")
	arr_term: str | None = Field(default=None, description="到达航站楼")
	dep_time: str | None = Field(default=None, description="出发时间 HH:MM")
	arr_time: str | None = Field(default=None, description="到达时间 HH:MM")
	arr_next_day: bool = Field(description="是否次日到达")
	duration_min: int | None = Field(default=None, description="飞行时长（分钟）")
	aircraft: str | None = Field(default=None, description="机型 IATA 代码")
	stop_count: int = Field(description="经停次数，0 表示直飞")
	is_code_share: bool = Field(description="是否代码共享")
	marketing_flight_no: str | None = Field(
		default=None, description="实际承运方航班号（代码共享时填）")
	lowest_total: float | None = Field(
		default=None, description="该航班当前舱位范围内的最低含税价")
	cabin_count: int = Field(description="该航班在库的舱位产品条数")


class PriceSummary(BaseModel):
	lowest: float | None = Field(default=None, description="最低含税价")
	highest: float | None = Field(default=None, description="最高含税价")
	avg: float | None = Field(default=None, description="平均含税价")


class EmptyInfo(BaseModel):
	"""
	空结果说明。

	以前 `count: 0` 什么也不说，前端只能猜；现在告诉调用方「为什么空」，
	以及要不要重试。
	"""
	reason: str = Field(description=(
		"no_flight 当天确实无航班 / no_data 近期多次无果 / "
		"collecting 正在采集 / throttled 限速中 / "
		"upstream_error 采集失败 / past_date 日期已过 / disabled 未开启"
	))
	message: str = Field(description="给用户看的中文说明")
	retryable: bool = Field(default=False, description="过一会儿重试是否有意义")
	retry_after: int | None = Field(
		default=None, description="建议多少秒后重试")


class SearchResponse(BaseModel):
	"""GET /api/flights/search"""
	dep: Side
	arr: Side
	date: str = Field(description="航班日期 yyyy-MM-dd")
	cabin: str = Field(description="舱位范围 Y / J / ALL")
	direct_only: bool = Field(description="是否只查了直飞")
	count: int = Field(description="航班数")
	flights: list[Flight]
	summary: PriceSummary
	empty: EmptyInfo | None = Field(
		default=None, description="count 为 0 时说明原因")


class TrendRow(BaseModel):
	"""走势的一行。by=date 时用 dep_date 等字段，by=time 时用 captured_at 等字段"""
	dep_date: str | None = Field(default=None, description="航班日期（by=date）")
	captured_at: str | None = Field(default=None, description="采集时刻（by=time）")
	lowest: float | None = Field(default=None, description="最低含税价")
	avg: float | None = Field(default=None, description="各航班最低价的平均")
	highest: float | None = Field(default=None, description="最高含税价")
	flights: int | None = Field(default=None, description="当日航班数（by=date）")
	rows_cnt: int | None = Field(default=None, description="价格条数（by=time）")


class TrendResponse(BaseModel):
	"""GET /api/flights/trend"""
	dep: Side
	arr: Side
	by: str = Field(description="date=按航班日期 / time=按采集时刻")
	cabin: str = Field(description="舱位类别 Y / J")
	rows: list[TrendRow]
	summary: dict = Field(description="by=date 给最便宜的一天；by=time 给涨跌变化")


class RouteItem(BaseModel):
	"""一条已采集的航线"""
	dep_iata: str
	arr_iata: str
	dep_airport: str
	arr_airport: str
	status: str = Field(description="active 表示近期采集到过数据")
	days: int = Field(description="覆盖的航班日期数")
	flights: int = Field(description="累计航班条数")


class CityItem(BaseModel):
	"""城市列表项，供前端选择器使用"""
	code: str = Field(description="城市三字码，如 BJS")
	name: str = Field(description="城市中文名，如 北京")
	airports: list[str] = Field(description="展开后的机场码，如 ['PEK','PKX']")
	initial: str = Field(description="首字母 A-Z，用于分组；# 表示非中文开头")
	pinyin: str = Field(description="全拼小写，用于按拼音搜索")
	is_hot: bool = Field(description="是否在采集范围内的热门城市")
	has_data: bool = Field(description="是否已采集到该城市的航班数据")


class CabinItem(BaseModel):
	"""一个舱位产品（一个航班可能有同舱位代码的多个产品）"""
	cabin_code: str = Field(description="舱位代码，如 L / V / Y / J")
	cabin_class: str | None = Field(
		default=None, description="产品名，如「新客特惠」「全价经济舱」")
	base_cabin: str = Field(description="舱位大类 Y / J")
	base_cabin_label: str = Field(description="大类中文名，如 经济舱")
	price: float | None = Field(default=None, description="票面价（不含税）")
	tax: float | None = Field(default=None, description="税费合计")
	price_total: float | None = Field(default=None, description="含税总价")
	seats_left: int | None = Field(
		default=None, description="余票数；seats_is_lower_bound 为真时表示「至少」")
	seats_is_lower_bound: bool = Field(
		description="余票是否为下限值（源数据 >9 表示至少 9 张）")
	fare_basis: str | None = Field(
		default=None, description="运价基础代码，区分同舱位代码下的不同产品")
	refund_change_type: str | None = Field(
		default=None, description="退改档位，如「退改费用高」")


class CabinFlight(BaseModel):
	"""舱位明细对应的航班信息"""
	flight_no: str
	dep_date: str = Field(description="航班日期 yyyy-MM-dd")
	arr_date: str | None = Field(default=None, description="到达日期，跨天时不同")
	dep_iata: str
	arr_iata: str
	dep_airport: str = Field(description="出发机场简称（含航站楼）")
	arr_airport: str
	dep_time: str | None
	arr_time: str | None
	arr_next_day: bool = Field(description="是否次日到达")
	duration_min: int | None
	aircraft: str | None
	aircraft_name: str | None
	stop_count: int = Field(description="经停次数，0 表示直飞")
	is_code_share: bool
	marketing_flight_no: str | None


class CabinResponse(BaseModel):
	"""GET /api/flights/{flight_no}/cabins"""
	flight: CabinFlight
	captured_at: str | None = Field(
		default=None, description="这批价格的采集时刻")
	cabin_count: int = Field(description="舱位产品数")
	cabins: list[CabinItem] = Field(
		description="按舱位大类（Y 先）→ 含税价升序排列")


class HealthResponse(BaseModel):
	status: str
	mysql: str
	version: str


# ============================================================
# 价格监控
# ============================================================

class MonitorCreate(BaseModel):
	"""创建监控规则"""
	dep: str = Field(description="出发城市码或机场码")
	arr: str = Field(description="到达城市码或机场码")
	date: str = Field(description="目标航班日期，2026-10-30 或 20261030")
	max_price: float = Field(gt=0, description="最高可接受含税价")
	cabin_scope: str = Field(default="Y", pattern="^(Y|J|ALL)$",
	                         description="舱位范围")
	notify_channel: str = Field(
		default="email", pattern="^(email|webhook)$",
		description="通知渠道：email 或 webhook")
	notify_to: str = Field(
		default="",
		description="接收通知的邮箱。发件人是系统配置的账号，这里填你自己的邮箱")
	notify_url: str = Field(
		default="", description="Webhook 地址，channel=webhook 时必填")


class MonitorUpdate(BaseModel):
	"""只传要改的字段"""
	max_price: float | None = Field(default=None, gt=0)
	cabin_scope: str | None = Field(default=None, pattern="^(Y|J|ALL)$")
	notify_channel: str | None = Field(default=None,
	                                   pattern="^(email|webhook)$")
	notify_to: str | None = Field(default=None, description="改收件邮箱")
	notify_url: str | None = Field(default=None, description="改 Webhook 地址")
	is_active: bool | None = Field(
		default=None, description="false 暂停，true 恢复并立刻重查")


class MonitorItem(BaseModel):
	"""监控规则 + 当前价格"""
	id: int
	dep_city: str
	arr_city: str
	dep_name: str
	arr_name: str
	target_date: str
	cabin_scope: str
	max_price: float
	current_price: float | None = Field(
		default=None, description="当前最低含税价，还没有数据时为 null")
	gap: float | None = Field(
		default=None, description="当前价 - 阈值，正数表示还差多少")
	triggered: bool = Field(description="当前价是否已经低于阈值")
	is_active: bool
	hit_count: int = Field(description="累计低于阈值的次数")
	check_interval_min: int | None = Field(
		default=None, description="当前检查间隔（按距起飞天数动态调整）")
	next_check_at: str | None = None
	last_checked_at: str | None = None
	last_notified_at: str | None = None
	notify_channel: str
	notify_to: str = Field(default="", description="通知收件邮箱")
	notify_url: str | None = Field(default=None, description="Webhook 地址")


class AlertItem(BaseModel):
	"""一条通知记录"""
	id: int
	flight_no: str
	cabin_code: str
	price_total: float
	max_price: float
	channel: str
	status: str = Field(description="pending / sent / failed")
	error_msg: str | None = None
	created_at: str
	sent_at: str | None = None


class LowestResponse(BaseModel):
	"""
	单航线最低价。**只读数据库，不触发采集。**

	前端的价格监控列表靠它刷新当前价：
	监控规则存在浏览器本地，这里只查「这条航线现在多少钱」这个公开信息，
	所以不需要传监控 id，也不可能看到别人的规则。
	"""
	dep: str
	arr: str
	date: str
	cabin: str
	lowest: float | None = Field(default=None, description="没有数据时为 null")
	flight_no: str | None = None
	dep_time: str | None = None
	arr_time: str | None = None
	cabin_code: str | None = None
	cabin_class: str | None = None
	captured_at: str | None = Field(
		default=None, description="这条价格的采集时间")
