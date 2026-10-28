# -*- coding: utf-8 -*-
"""
==*==*==*==*==*==*==*==*==*==*==*==*==*==*==*==
@FileName: __init__.py
@Remark: 静态数据包

city_code.py 是从原 core/cityCode.py 原样迁移过来的静态字典，
内容较大（约 360 KB），不建议手工修改。

    AirPortCodeData  key = 机场三字码，value = {cityCode, airportCnName, ...}
    CityCodeData     key = 城市三字码，value = {cityCnName, airports, airportCount}
==*==*==*==*==*==*==*==*==*==*==*==*==*==*==*==
"""
from csair.data.city_code import AirPortCodeData, CityCodeData

__all__ = ["AirPortCodeData", "CityCodeData"]
