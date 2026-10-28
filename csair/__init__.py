# -*- coding: utf-8 -*-
"""
==*==*==*==*==*==*==*==*==*==*==*==*==*==*==*==
@FileName: __init__.py
@Remark: 南航航班数据采集系统

模块划分：
    settings      配置（城市列表、日期范围、延迟、token 路径）
    exceptions    业务异常（CollectError）
    database      MySQL / MongoDB 连接封装
    assets        城市、机场主数据与映射
    utils         通用工具（时间、请求头、gzip）
    parser        原始响应 -> 结构化 dict
    spider        采集层（HTTP 请求 + 风控处理）
    storage       入库层（MySQL 写入、解析编排）
    celery_app    Celery 配置与定时任务表
    jobs          Celery 任务定义
    cli.query     命令行查询工具
    cli.check     每日巡检

入口：
    celery -A csair.celery_app worker -l info -c 1 --pool=solo
    celery -A csair.celery_app beat -l info
    python -m csair.cli.query search --dep CSX --arr BJS --date 2026-10-28
    python -m csair.cli.check
==*==*==*==*==*==*==*==*==*==*==*==*==*==*==*==
"""

__version__ = "1.0.0"
__all__ = []
