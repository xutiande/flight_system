# -*- coding: utf-8 -*-

from contextlib import contextmanager
from datetime import datetime
from typing import Iterator, Optional

import pymysql
from loguru import logger
from pymongo import ASCENDING, MongoClient
from pymongo.errors import PyMongoError
from pymysql.cursors import DictCursor

from csair.settings import MONGO, MYSQL


# ============================================================
# MySQL
# ============================================================

class MySQLDB:

	def __init__(self, **kwargs):
		cfg = {**MYSQL, **kwargs}
		self.host = cfg["host"]
		self.port = cfg["port"]
		self.user = cfg["user"]
		self.password = cfg["password"]
		self.database = cfg["database"]
		self.charset = cfg["charset"]
		self._conn = None

		self._ensure_database()
		self.connect()

	# ---------- 建库 ----------
	def _ensure_database(self):
		"""连到 MySQL 但不指定库，确保目标数据库存在"""
		conn = pymysql.connect(
			host=self.host,
			port=self.port,
			user=self.user,
			password=self.password,
			charset=self.charset,
		)
		try:
			with conn.cursor() as cursor:
				cursor.execute(
					f"create database if not exists `{self.database}` "
					f"default character set utf8mb4 "
					f"collate utf8mb4_general_ci"
				)
			conn.commit()
		finally:
			conn.close()

	# ---------- 连接管理 ----------
	def connect(self):
		self._conn = pymysql.connect(
			host=self.host,
			port=self.port,
			user=self.user,
			password=self.password,
			database=self.database,
			charset=self.charset,
			cursorclass=DictCursor,
			autocommit=False,
		)
		return self

	def close(self):
		if self._conn:
			self._conn.close()
			self._conn = None

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

	# ---------- 游标 / 事务上下文 ----------
	@contextmanager
	def cursor(self):
		"""
		yield 一个游标，块正常结束提交，异常回滚，最后关闭游标。

		注意：不要在同一个连接上嵌套使用两个 cursor()，
		否则会变成两个独立事务，破坏原子性。
		"""
		try:
			self._conn.ping(reconnect=False)
		except Exception:
			# 连接已断开（空闲超时 / 服务端重启），重连一次
			self.connect()

		conn = self._conn
		cur = conn.cursor()
		try:
			yield cur
			conn.commit()
		except Exception:
			conn.rollback()
			raise
		finally:
			cur.close()

	# ---------- 通用查询 ----------
	def query(self, sql, params=None):
		"""查询多条记录"""
		with self.cursor() as cur:
			cur.execute(sql, params or ())
			return cur.fetchall()

	def query_one(self, sql, params=None):
		"""查询单条记录"""
		with self.cursor() as cur:
			cur.execute(sql, params or ())
			return cur.fetchone()

	def execute(self, sql, params=None):
		"""执行增删改，返回受影响行数"""
		with self.cursor() as cur:
			return cur.execute(sql, params or ())

	def execute_many(self, sql, params_list):
		"""批量执行"""
		with self.cursor() as cur:
			return cur.executemany(sql, params_list)

	def insert(self, sql, params=None):
		"""插入并返回自增 ID"""
		with self.cursor() as cur:
			cur.execute(sql, params or ())
			return cur.lastrowid

	def call_proc(self, proc_name, params=None):
		"""调用存储过程"""
		with self.cursor() as cur:
			cur.callproc(proc_name, params or ())
			return cur.fetchall()


# ============================================================
# MongoDB
# ============================================================

class MongoDB:
	"""raw_responses 集合的读写封装"""

	def __init__(self, **kwargs):
		cfg = {**MONGO, **kwargs}
		self.database_name = cfg["database"]
		self.col_name = cfg["collection"]

		self.client = MongoClient(
			host=cfg["host"],
			port=cfg["port"],
			username=cfg["username"],
			password=cfg["password"],
			authSource=cfg["auth_source"],
			maxPoolSize=cfg["max_pool_size"],
		)
		self.db = self.client[self.database_name]
		self.col = self.db[self.col_name]

	# ---------- 资源管理 ----------
	def close(self):
		self.client.close()

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

	# ---------- 索引初始化（启动时调用一次） ----------
	def ensure_indexes(self):
		"""建索引。幂等，重复调用无副作用"""
		self.col.create_index([("sign", ASCENDING)], unique=True, name="uk_sign")
		self.col.create_index(
			[("captured_at", ASCENDING)],
			expireAfterSeconds=604800,
			name="ttl_7d",
		)
		self.col.create_index(
			[("dep_city", 1), ("arr_city", 1), ("dep_date", 1), ("captured_at", -1)],
			name="idx_route_date",
		)
		self.col.create_index(
			[("parse_status", 1), ("captured_at", -1)],
			name="idx_parse_status",
		)
		logger.info(f"Mongo 索引已就绪：{self.col_name}")

	# ---------- 写入 ----------
	def upsertOne(self, data: dict, colName: str = None) -> dict:
		"""按 sign 幂等写入。已存在则更新"""
		col = self.db[colName] if colName else self.col
		try:
			result = col.update_one(
				{"sign": data["sign"]},
				{"$set": data},
				upsert=True,
			)
		except PyMongoError as e:
			logger.error(f"数据 <{data.get('sign')}> 写入异常：{e}")
			raise

		if result.upserted_id:
			return {"status": "inserted", "sign": data["sign"],
			        "id": str(result.upserted_id)}

		return {
			"status": "updated",
			"sign": data["sign"],
			"matched_count": result.matched_count,
			"modified_count": result.modified_count,
		}

	# ---------- 更新 ----------
	def updateOne(self, filterDict: dict, updateDict: dict,
	              colName: str = None) -> dict:
		"""部分更新。文档不存在时 matched_count = 0，不会插入"""
		col = self.db[colName] if colName else self.col
		try:
			result = col.update_one(filterDict, {"$set": updateDict})
		except PyMongoError as e:
			logger.error(f"更新异常 {filterDict}：{e}")
			raise

		if result.matched_count == 0:
			logger.warning(f"更新命中 0 条：{filterDict}")

		return {
			"matched_count": result.matched_count,
			"modified_count": result.modified_count,
		}

	def markParsed(self, sign: str, status: str = "success",
	               error: str = None, data_is_none: bool = None) -> bool:
		"""
		回写解析状态。

		status        : success / failed / skipped
		data_is_none  : 解析结果是否为空；None 表示不改动（失败路径不传）
		"""
		update = {
			"parse_status": status,
			"parse_error": (error or "")[:500] or None,
			"parsed_at": datetime.now(),
		}
		if data_is_none is not None:
			update["data_is_none"] = data_is_none

		res = self.updateOne({"sign": sign}, update)
		return res["matched_count"] == 1

	# ---------- 查询 ----------
	def findOne(self, filterDict: dict) -> Optional[dict]:
		return self.col.find_one(filterDict)

	def findAll(self, filterDict: dict = None,
	            batch_size: int = 100) -> Iterator[dict]:
		cursor = self.col.find(filterDict or {}).batch_size(batch_size)
		try:
			for doc in cursor:
				yield doc
		finally:
			cursor.close()

	def count(self, filterDict: dict = None) -> int:
		return self.col.count_documents(filterDict or {})
