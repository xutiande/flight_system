# -*- coding: utf-8 -*-

import os
import sys

# ---------------------------------------------------------------------------
# 把项目根目录永久加入 sys.path
#
# celery 加载 `-A` 指定的模块时，用 cwd_in_path() 临时插入当前目录，
# 加载完立即移除。若任务函数内部还有「延迟 import」，此时路径已不在。
# 用「带尾分隔符」的形式插入，字符串与 cwd 不同，不会被 remove() 命中。
#
# 更稳的做法仍是启动前 set PYTHONPATH=%CD%（见文件头注释）。
# ---------------------------------------------------------------------------
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT_SLASHED = _PROJECT_ROOT + os.sep
if _PROJECT_ROOT_SLASHED not in sys.path:
	sys.path.insert(0, _PROJECT_ROOT_SLASHED)

from celery import Celery
from celery.schedules import crontab

from csair.settings import REDIS

app = Celery(
	"csair",
	broker=f"redis://{REDIS['host']}:{REDIS['port']}/{REDIS['broker_db']}",
	backend=f"redis://{REDIS['host']}:{REDIS['port']}/{REDIS['backend_db']}",
	include=["csair.jobs"],
)

app.conf.update(
	# ---------- 时区 ----------
	timezone="Asia/Shanghai",
	enable_utc=False,

	# ---------- 任务可靠性 ----------
	task_acks_late=True,             # worker 崩溃时任务回队列，不丢
	task_reject_on_worker_lost=True,
	worker_prefetch_multiplier=1,    # 长任务不允许被单个 worker 囤积
	task_ignore_result=True,         # 采集任务不需要取结果，省 Redis 内存

	# 可见性超时，必须大于最长任务耗时（采集含 10~20s 休眠）
	broker_transport_options={"visibility_timeout": 1800},
	broker_connection_retry_on_startup=True,

	# ---------- 定时任务 ----------
	beat_schedule={
		# ① 每天 17:30 生成未来 N 天的采集任务
		"generate-daily": {
			"task": "csair.jobs.generate_collect_tasks",
			"schedule": crontab(hour=17, minute=30),
		},
		# ② 每分钟把 pending 任务派发给 worker
		"dispatch-pending": {
			"task": "csair.jobs.dispatch_pending",
			"schedule": 60.0,
		},
		# ③ 每 5 分钟把 Mongo 里 waiting 的响应解析入库
		"parse-pending": {
			"task": "csair.jobs.parse_pending",
			"schedule": 300.0,
		},
		# ④ 每 5 分钟把到期的价格监控规则转成采集任务
		"dispatch-monitor": {
			"task": "csair.jobs.dispatch_monitor",
			"schedule": 300.0,
		},
	},
)
