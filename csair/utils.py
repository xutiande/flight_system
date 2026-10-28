# -*- coding: utf-8 -*-

import gzip
import hashlib
import json
import re
from datetime import datetime, timedelta

from loguru import logger


# ============================================================
# 时间
# ============================================================

def formatDateStr(dateStr: str) -> str:
	"""20261028 -> 2026-10-28"""
	return datetime.strptime(dateStr, "%Y%m%d").date().strftime("%Y-%m-%d")


def toCompactDate(dateStr: str) -> str:
	"""2026-10-28 -> 20261028"""
	return datetime.strptime(dateStr, "%Y-%m-%d").strftime("%Y%m%d")


def get_dates_between(start_date: str, end_date: str, fmt: str = "%Y%m%d") -> list:
	"""取两个日期之间的所有日期（含首尾）"""
	start = datetime.strptime(start_date, fmt)
	end = datetime.strptime(end_date, fmt)
	return [
		(start + timedelta(days=i)).strftime(fmt)
		for i in range((end - start).days + 1)
	]


def addDays(date_str: str, days: int = 1, fmt: str = "%Y%m%d") -> str:
	date = datetime.strptime(date_str, fmt)
	return (date + timedelta(days=days)).strftime(fmt)


def hourBucket(now: datetime = None) -> datetime:
	"""
	截断到小时，作为价格快照的幂等键。

	同一小时内重复采集会命中同一个唯一键，走 UPDATE 而不是 INSERT，
	跨小时才产生新快照。
	"""
	now = now or datetime.now()
	return now.replace(minute=0, second=0, microsecond=0)


# ============================================================
# 请求头
# ============================================================

def buildHeaders(userAgent: str) -> dict:
	return {
		"Host": "m.csair.com",
		"cache-control": "no-cache",
		"origin": "https://m.csair.com",
		"user-agent": userAgent,
		"content-type": "application/json; charset=UTF-8",
		"accept": "*/*",
		"sec-fetch-site": "same-origin",
		"sec-fetch-mode": "cors",
		"referer": "https://m.csair.com/booking_new/",
		"accept-language": "zh-CN,zh;q=0.9",
	}


def buildUserAgent(userAgent: str) -> str:
	"""
	从 UA 里提取 “Mozilla-5.0_AppleWebKit-537.36_...” 形式的串，
	用作接口的 osversion 参数。
	"""
	parts = re.findall(
		r'(Mozilla|AppleWebKit|Chrome|Mobile|Safari)(?:/([\d.]+))?', userAgent
	)
	result = []
	for name, ver in parts:
		result.append(f"{name}-{ver}" if ver else name)
	return "_".join(result)


def matchArg(text: str):
	"""从反爬挑战页里提取 40 位 arg（acw_sc__v2 的入参）"""
	arg = re.findall("[0-9A-Z]{40}", text)
	if not arg:
		logger.warning(f"正则匹配 acw_sc__v2 arg 失败：{text[:50]}...")
		return None
	return arg[0]


# ============================================================
# 摘要
# ============================================================

def md5Encrypt(text: str) -> str:
	return hashlib.md5(text.encode()).hexdigest()


def make_sign(date: str, dep_city: str, arr_city: str, hour: str) -> str:
	"""
	原始响应的幂等键。

	包含小时粒度，保证：
	    - 同一小时内重复采集覆盖同一条
	    - 跨小时是不同快照，价格历史不会丢
	"""
	return md5Encrypt(f"{date}{dep_city}{arr_city}{hour}")


# ============================================================
# 压缩
# ============================================================

class ZipData:
	"""gzip + json 压缩，用于把大体积响应存进 MongoDB"""

	@staticmethod
	def zipData(data: dict):
		"""
		返回 {raw_bytes, zip_bytes, encoding, body}
		body 是压缩后的二进制，直接存 BSON Binary
		"""
		try:
			raw = json.dumps(data).encode()
			compressed = gzip.compress(raw)
			return {
				"raw_bytes": len(raw),   # 解压后字节数，用于校验
				"zip_bytes": len(compressed),
				"encoding": "gzip+json",
				"body": compressed,
			}
		except Exception as e:
			logger.error(f"压缩失败：{e}")
			return None

	@staticmethod
	def unZip(data: dict):
		"""
		返回 {'status': 大小是否一致, 'body': 解压后的 dict}
		解压或解析失败返回 None

		字段兼容：历史文档用 start_size / end_size（同样是整数字节），
		新代码用 raw_bytes / zip_bytes。两种都认。
		"""
		try:
			decompressed = gzip.decompress(data["body"])
			restored = json.loads(decompressed.decode("utf-8"))

			# 迁移来的老数据没有精确字节数，跳过严格校验
			if data.get("legacy"):
				return {"status": True, "body": restored}

			expected = data.get("raw_bytes")
			if expected is None:
				expected = data.get("start_size")

			status = True if expected is None else (len(decompressed) == int(expected))
			return {"status": status, "body": restored}
		except Exception as e:
			logger.error(f"{data.get('sign')} 解压失败：{e}")
			return None


def get_acw(t):
	def unsbox(t):
		r = [15, 35, 29, 24, 33, 16, 1, 38, 10, 9, 19, 31, 40, 27, 22, 23, 25, 13, 6, 11, 39, 18, 20, 8, 14, 21, 32, 26,
			 2, 30, 7, 4, 17, 5, 3, 28, 34, 37, 12, 36]
		e = [""] * len(r)
		for n in range(len(t)):
			o = t[n]
			for w in range(len(r)):
				if r[w] == n + 1:
					e[w] = o
		return "".join(e)
	def hex_xor(s1, s2):
		result = ""
		for i in range(0, min(len(s1), len(s2)), 2):
			# 取出两个字符串的两位十六进制数
			h1 = int(s1[i:i+2], 16)
			h2 = int(s2[i:i+2], 16)
			# 进行异或操作并转换为十六进制字符串
			xor_result = hex(h1 ^ h2)[2:]
			# 如果结果长度为1，补0
			if len(xor_result) == 1:
				xor_result = "0" + xor_result
			result += xor_result
		return result
	r = "3000176000856006061501533003690027800375"
	e = unsbox(t)
	arg2 = hex_xor(e, r)
	return arg2