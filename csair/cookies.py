# -*- coding: utf-8 -*-
import json

import redis
from loguru import logger

from csair.settings import REDIS, load_token

COOKIE_KEY = "csair:cookies"
COOKIE_TTL = 86400   # 1 天，acw cookie 不会活得更久


def _cookie_store():
	"""db 8 专用连接。decode_responses=True 直接拿字符串"""
	return redis.Redis(
		host=REDIS["host"], port=REDIS["port"], db=REDIS["cookies_db"],
		socket_connect_timeout=2, decode_responses=True,
	)


def load_cookies() -> dict:
	"""
	取当前 token 对应的 cookies。

	Redis 里没有、或者存的是旧 token 的 cookie，就用初始 cookie 覆盖。
	"""
	token = load_token()

	try:
		raw = _cookie_store().get(COOKIE_KEY)
	except Exception as e:
		logger.warning(f"[Cookie] 读取 Redis 失败，使用初始 cookie：{e}")
		return {"TOKEN": token}

	if raw:
		try:
			data = json.loads(raw)
		except (TypeError, json.JSONDecodeError):
			data = {}

		if data.get("token") == token and isinstance(data.get("cookies"), dict):
			return data["cookies"]

		logger.info("[Cookie] token 已变更，丢弃 Redis 里的旧 cookie")

	cookies = {"TOKEN": token}
	save_cookies(cookies)
	return cookies


def save_cookies(cookies: dict):
	"""
	把采集过程中刷新过的 cookie 写回 Redis。

	写失败只告警，不影响本次采集结果。
	"""
	cookies = dict(cookies or {})
	token = cookies.get("TOKEN")
	if not token:
		return

	payload = json.dumps(
		{"token": token, "cookies": cookies},
		ensure_ascii=False,
	)
	try:
		_cookie_store().set(COOKIE_KEY, payload, ex=COOKIE_TTL)
	except Exception as e:
		logger.warning(f"[Cookie] 写入 Redis 失败：{e}")
