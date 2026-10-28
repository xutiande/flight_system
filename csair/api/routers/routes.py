# -*- coding: utf-8 -*-
from fastapi import APIRouter, Query

from csair.api.schemas import RouteItem
from csair.queries import list_routes

router = APIRouter(prefix="/api/routes", tags=["routes"])


@router.get(
	"",
	response_model=list[RouteItem],
	summary="列出已采集且有数据的航线",
)
def routes(
	limit: int = Query(default=0, ge=0, le=1000,
	                   description="最多返回几条，0 表示全部"),
):
	"""
	按航班数倒序。`days` 是覆盖的航班日期数，`flights` 是累计航班条数。
	"""
	rows = list_routes()
	return rows[:limit] if limit else rows
