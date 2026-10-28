# -*- coding: utf-8 -*-
"""
==*==*==*==*==*==*==*==*==*==*==*==*==*==*==*==
@FileName: __init__.py
@Remark: HTTP 接口层

启动：
    uvicorn csair.api.main:app --host 0.0.0.0 --port 8000 --reload

分层：
    api/        参数校验、JSON 序列化、HTTP 状态码
    queries.py  拼 SQL、执行、返回中性 dict
    两边共用同一份数据口径，不允许各自写 SQL。

约束：
    本包不 import csair.jobs / csair.celery_app。
    那会连锁建立 Redis / Celery 连接，让 API 进程无端依赖调度组件。
==*==*==*==*==*==*==*==*==*==*==*==*==*==*==*==
"""
