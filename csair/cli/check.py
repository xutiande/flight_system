# -*- coding: utf-8 -*-

import argparse
import sys
from datetime import datetime, timedelta

from csair.database import MongoDB, MySQLDB

try:
	sys.stdout.reconfigure(encoding="utf-8")
except Exception:
	pass

LINE = "=" * 62
ICON = {"ok": "[OK]  ", "warn": "[WARN]", "bad": "[FAIL]", "skip": "[ -- ]"}


# ============================================================
# 各项检查
# ============================================================

def check_tasks(db, day):
	"""
	到期任务完成率。

	generate 在 D 日生成 D / D+1 / D+2 三天的任务，所以检查 D 日批次时，
	D+1、D+2 的任务本来就还没到采集时间。只统计 dep_date <= 今天的部分。
	"""
	row = db.query_one(
		"""
		select count(*)                             as total,
		       coalesce(sum(status = 'success'), 0) as ok,
		       coalesce(sum(status = 'failed'), 0)  as failed,
		       coalesce(sum(status = 'expired'), 0) as expired,
		       coalesce(sum(status in ('pending', 'queued', 'running')), 0) as unfinished
		from crawl_task
		where date(created_at) = %s
		  and dep_date <= curdate()
		""",
		(day,),
	)
	total = int(row["total"] or 0)
	ok = int(row["ok"] or 0)
	failed = int(row["failed"] or 0)
	expired = int(row["expired"] or 0)
	unfinished = int(row["unfinished"] or 0)

	rate = (ok / total * 100) if total > 0 else 0.0

	if total == 0:
		status, hint = "skip", "没有已到期的任务"
	elif rate >= 95:
		status, hint = "ok", ""
	elif rate >= 85:
		status, hint = "warn", "有任务未完成，检查 worker 是否中途停止"
	else:
		status = "bad"
		hint = (f"{expired} 条因日期过期未采（采集速度跟不上或中途停过）"
		        if expired > 0 else "完成率过低，重点看是否有大量 failed")

	return {
		"status": status,
		"value": f"{rate:.1f}%",
		"detail": f"{ok} 成功 / {total} 到期",
		"extra": f"失败 {failed} · 过期 {expired} · 待采 {unfinished}",
		"hint": hint,
	}


def check_parse_rate(db, day):
	"""解析成功率 = 解析出的航班数 / 响应里的原始航班数"""
	row = db.query_one(
		"""
		select coalesce(sum(raw_items), 0)   as raw_total,
		       coalesce(sum(parsed_rows), 0) as parsed_total
		from crawl_task
		where status = 'success'
		  and date(created_at) = %s
		  and raw_items > 0
		""",
		(day,),
	)
	raw_total = int(row["raw_total"] or 0)
	parsed_total = int(row["parsed_total"] or 0)

	if raw_total == 0:
		return {"status": "skip", "value": "-", "detail": "没有可解析的数据",
		        "extra": "", "hint": ""}

	rate = parsed_total / raw_total * 100
	if rate >= 95:
		status, hint = "ok", ""
	elif rate >= 85:
		status, hint = "warn", "部分响应解析不完整，抽样看 raw_items > parsed_rows 的记录"
	else:
		status, hint = "bad", "解析大面积失败，大概率是接口结构变了"

	return {
		"status": status,
		"value": f"{rate:.1f}%",
		"detail": f"原始 {raw_total} → 解析 {parsed_total}",
		"extra": "", "hint": hint,
	}


def check_empty_ratio(db, day):
	"""空结果占比。过高说明可能被风控"""
	row = db.query_one(
		"""
		select coalesce(sum(raw_items = 0), 0) as empty_n,
		       count(*)                        as total
		from crawl_task
		where status = 'success' and date(created_at) = %s
		""",
		(day,),
	)
	empty_n = int(row["empty_n"] or 0)
	total = int(row["total"] or 0)

	if total == 0:
		return {"status": "skip", "value": "-", "detail": "没有成功任务",
		        "extra": "", "hint": ""}

	ratio = empty_n / total * 100
	if ratio <= 30:
		status, hint = "ok", ""
	elif ratio <= 50:
		status, hint = "warn", "空结果偏多，抽几条原始响应确认是真的没航班"
	else:
		status = "bad"
		hint = "空结果超过一半，极可能是被风控（结构完整但列表为空）"

	return {
		"status": status,
		"value": f"{ratio:.1f}%",
		"detail": f"{empty_n} 空 / {total} 成功",
		"extra": "", "hint": hint,
	}


def check_freshness(db):
	"""数据新鲜度：最后一次成功采集距今多久"""
	row = db.query_one(
		"select max(started_at) as last_success from crawl_task where status = 'success'"
	)
	last = row["last_success"] if row else None

	if not last:
		return {"status": "bad", "value": "无记录", "detail": "从未成功采集过",
		        "extra": "", "hint": "检查 worker / beat 是否在运行"}

	minutes = (datetime.now() - last).total_seconds() / 60
	if minutes <= 30:
		status, hint = "ok", ""
	elif minutes <= 120:
		status, hint = "warn", "超过 2 小时没有成功采集，确认采集时段是否已结束"
	else:
		status, hint = "bad", "长时间没有成功采集，检查 token / worker / 网络"

	return {
		"status": status,
		"value": f"{int(minutes)} 分钟前",
		"detail": f"最后成功 {last:%Y-%m-%d %H:%M}",
		"extra": "", "hint": hint,
	}


def check_token(db):
	"""Token 状态：靠最近成功时间和最近失败数间接判断"""
	last_ok = db.query_one(
		"select max(started_at) as t from crawl_task where status = 'success'"
	)["t"]
	failed_24h = int(db.query_one(
		"""
		select coalesce(sum(status = 'failed'), 0) as n
		from crawl_task
		where created_at >= now() - interval 1 day
		"""
	)["n"] or 0)

	if not last_ok:
		return {"status": "bad", "value": "无成功记录",
		        "detail": f"24h 失败 {failed_24h} 条",
		        "extra": "", "hint": "检查 info/token.txt 与登录态"}

	gap_hours = (datetime.now() - last_ok).total_seconds() / 3600

	if failed_24h > 20:
		status = "bad"
		hint = "失败任务过多，大概率 token 已过期：更新 info/token.txt 后重置失败任务"
	elif gap_hours <= 2:
		status, hint = "ok", ""
	elif gap_hours <= 6:
		status, hint = "warn", "较长时间无成功记录，留意 token 是否临近过期"
	else:
		status = "bad"
		hint = "超过 6 小时没有成功采集，token 很可能已失效"

	return {
		"status": status,
		"value": f"{gap_hours:.1f} 小时前成功",
		"detail": f"24h 内失败 {failed_24h} 条",
		"extra": "", "hint": hint,
	}


def check_mongo_backlog():
	"""Mongo 待解析积压"""
	try:
		mongo = MongoDB()
		waiting = mongo.count({"parse_status": "waiting"})
		failed = mongo.count({"parse_status": "failed"})
	except Exception as e:
		return {"status": "bad", "value": "连接失败", "detail": str(e)[:60],
		        "extra": "", "hint": "检查 MongoDB 是否在运行"}

	if waiting <= 20:
		status, hint = "ok", ""
	elif waiting <= 200:
		status, hint = "warn", "解析进程落后于采集，检查 parse_pending 是否在跑"
	else:
		status, hint = "bad", "积压严重，parse_pending 可能没运行"

	return {
		"status": status,
		"value": f"{waiting} 条待解析",
		"detail": f"failed {failed} 条",
		"extra": "", "hint": hint,
	}


def check_total(db):
	"""各表总量"""
	rows = db.query(
		"""
		select 'flight'          as t, count(*) as n from flight
		union all select 'price_snapshot', count(*) from price_snapshot
		union all select 'route',          count(*) from route
		union all select 'crawl_task',     count(*) from crawl_task
		"""
	)
	return {r["t"]: int(r["n"]) for r in rows}


# ============================================================
# 输出
# ============================================================

def render_item(label, result):
	icon = ICON.get(result["status"], "[????]")
	print(f"  {icon} {label:<14} {result['value']:<16} {result['detail']}".rstrip())
	if result.get("extra"):
		print(f"         {'':<14} {result['extra']}")
	if result.get("hint"):
		print(f"         -> {result['hint']}")


def main():
	parser = argparse.ArgumentParser(description="南航采集系统每日巡检")
	parser.add_argument("--date", help="检查哪一天创建的批次，默认昨天")
	args = parser.parse_args()

	day = (datetime.strptime(args.date, "%Y-%m-%d").date()
	       if args.date else
	       (datetime.now() - timedelta(days=1)).date())

	db = MySQLDB()

	print()
	print(LINE)
	print("  南航采集系统 · 每日巡检")
	print(f"  批次日期：{day}      运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
	print(LINE)
	print()

	results = []

	print("【采集任务】")
	for label, fn in [("完成率", lambda: check_tasks(db, day)),
	                  ("数据新鲜度", lambda: check_freshness(db))]:
		r = fn()
		results.append((label, r))
		render_item(label, r)

	print()
	print("【数据质量】")
	for label, fn in [("解析成功率", lambda: check_parse_rate(db, day)),
	                  ("空结果占比", lambda: check_empty_ratio(db, day))]:
		r = fn()
		results.append((label, r))
		render_item(label, r)

	print()
	print("【Token】")
	r = check_token(db)
	results.append(("Token 状态", r))
	render_item("Token 状态", r)

	print()
	print("【MongoDB】")
	r = check_mongo_backlog()
	results.append(("待解析积压", r))
	render_item("待解析积压", r)

	print()
	print("【数据总量】")
	try:
		for t, n in check_total(db).items():
			print(f"         {t:<16} {n:>10,}")
	except Exception as e:
		print(f"         查询失败：{e}")

	bad = [x for x in results if x[1]["status"] == "bad"]
	warn = [x for x in results if x[1]["status"] == "warn"]

	print()
	print(LINE)
	if bad:
		print(f"  {len(bad)} 项异常：" + "、".join(x[0] for x in bad))
		if warn:
			print(f"  {len(warn)} 项需关注：" + "、".join(x[0] for x in warn))
	elif warn:
		print(f"  {len(warn)} 项需关注：" + "、".join(x[0] for x in warn))
	else:
		print("  全部正常")
	print(LINE)
	print()

	return 1 if bad else 0


if __name__ == "__main__":
	sys.exit(main())
