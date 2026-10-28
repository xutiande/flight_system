# -*- coding: utf-8 -*-
from fastapi import APIRouter

from csair.api.schemas import (
	AlertItem,
	MonitorCreate,
	MonitorItem,
	MonitorUpdate,
)
from csair.database import MySQLDB
from csair.monitor import (
	create_rule,
	delete_rule,
	list_alerts,
	list_rules,
	update_rule,
)

router = APIRouter(prefix="/api/monitors", tags=["monitors"])

DEFAULT_USER_ID = 1


@router.post("", response_model=dict, status_code=201, summary="创建监控规则")
def create(payload: MonitorCreate):
	"""
	同一条 (出发, 到达, 日期) 只能有一条启用中的规则。
	创建后 `next_check_at` 为当前时间，下一轮派发就会采一次。

	`notify_to` 填**你自己的邮箱**：系统用自己的发件账号把降价通知发给你。
	"""
	db = MySQLDB()
	rule_id = create_rule(
		db,
		dep=payload.dep,
		arr=payload.arr,
		target_date=payload.date,
		max_price=payload.max_price,
		cabin_scope=payload.cabin_scope,
		notify_channel=payload.notify_channel,
		notify_to=payload.notify_to,
		notify_url=payload.notify_url,
		user_id=DEFAULT_USER_ID,
	)
	return {"id": rule_id}


@router.get("", response_model=list[MonitorItem], summary="监控规则列表")
def index(include_inactive: bool = False):
	"""
	返回每条规则的当前最低价与差距，前端据此显示「还差多少」。
	"""
	db = MySQLDB()
	return list_rules(db, include_inactive=include_inactive,
	                  user_id=DEFAULT_USER_ID)


@router.patch("/{rule_id}", response_model=dict, summary="修改规则")
def update(rule_id: int, payload: MonitorUpdate):
	"""
	可改阈值、舱位范围、收件邮箱、通知渠道；
	`is_active=true` 恢复监控并立刻重查。
	"""
	db = MySQLDB()
	ok = update_rule(
		db, rule_id,
		max_price=payload.max_price,
		cabin_scope=payload.cabin_scope,
		notify_channel=payload.notify_channel,
		notify_to=payload.notify_to,
		notify_url=payload.notify_url,
		is_active=payload.is_active,
	)
	return {"updated": ok}


@router.delete("/{rule_id}", response_model=dict, summary="停用规则")
def remove(rule_id: int):
	"""软删除：置 is_active=0，历史告警记录保留"""
	db = MySQLDB()
	return {"deleted": delete_rule(db, rule_id)}


@router.get("/{rule_id}/alerts", response_model=list[AlertItem],
            summary="通知历史")
def alerts(rule_id: int, limit: int = 20):
	db = MySQLDB()
	return list_alerts(db, rule_id, limit=min(max(limit, 1), 100))
