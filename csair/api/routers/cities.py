# -*- coding: utf-8 -*-

from fastapi import APIRouter

from csair.api.schemas import CityItem
from csair.queries import list_cities

router = APIRouter(prefix="/api/cities", tags=["cities"])


@router.get("", response_model=list[CityItem],
            summary="列出有航班数据的城市")
def cities():
	"""
	按城市中文名排序。`airports` 是该城市展开后的机场码，
	查询时直接传 `code` 就能覆盖所有机场。
	"""
	return list_cities()
