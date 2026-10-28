# -*- coding: utf-8 -*-
import base64
import json
import re
import time

import requests
from loguru import logger

from csair import settings
from csair.exceptions import CollectError
from csair.utils import buildHeaders, buildUserAgent, matchArg, get_acw

requests.packages.urllib3.disable_warnings()


# ============================================================
# 风控服务（获取 acw_sc__v2、校验验证码）
# ============================================================

class RiskApi:
	@staticmethod
	def getAcw(arg):
		return get_acw(arg)

	@staticmethod
	def verifyCaptcha(traceid, userAgent, proxy):
		"""校验验证码。失败会重试一次"""
		url = settings.RISK_SERVICE + "/getSign"
		try:
			params = {
				"userAgent": base64.b64encode(userAgent.encode()).decode(),
				"traceId": traceid,
				"proxy": proxy,
			}
			for count in range(1, 3):
				response = requests.get(url, params=params)
				jsonData = response.json()
				if jsonData["status"] != 1:
					logger.warning(
						f"验证码校验失败：{jsonData}，重连次数：{count}"
					)
					continue
				logger.success(f"验证码校验成功：{traceid}.")
				return True
			return False
		except Exception as e:
			logger.error(f"风控地址 </getSign> 请求失败：{e}")
			return False


# ============================================================
# 采集客户端
# ============================================================

class FlightSpider:
	"""
	航班查询客户端。

	用法：
	    spider = FlightSpider({"TOKEN": token})
	    payload = spider.fetch_flights("20261028", "CSX", "BJS")
	    payload["json"]       原始响应字典
	    payload["http_status"]
	    payload["data_is_none"]  响应结构完整但没有航班
	"""

	def __init__(self, cookies: dict, userAgent: str = None, proxy: str = None):
		self.userAgent = userAgent or settings.USER_AGENT
		self.proxy = proxy
		self.headers = buildHeaders(self.userAgent)
		self.cookies = dict(cookies or {})
		self.session = requests.Session()
		if proxy:
			self.session.proxies.update({
				"http": "http://" + proxy,
				"https": "http://" + proxy,
			})
		self.session.trust_env = False

	def close(self):
		self.session.close()

	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_val, exc_tb):
		self.close()

	# ---------- 主流程 ----------
	def fetch_flights(self, date: str, dep_city: str, arr_city: str) -> dict:
		"""
		查询国内航班。

		:param date: 查询日期，20261028 格式
		:param dep_city: 出发城市三字码
		:param arr_city: 到达城市三字码
		:return: {"json": dict, "http_status": int, "data_is_none": bool}
		:raises CollectError: 任何拿不到有效响应的情况
		"""
		body = {
			"isLogin": True,
			"ffpNo": "",
			"adultNum": 1,
			"childNum": 0,
			"infantNum": 0,
			"flightType": "",
			"date": date,
			"cities": {"depCity": dep_city, "arrCity": arr_city},
			"depCityFlag": True,
			"arrCityFlag": True,
			"segType": "S",
		}
		params = {
			"type": "MOBILE",
			"APPTYPE": "touch",
			"chanel": "touch",
			"lang": "zh",
			"_": "",
			"timeZone": "GMT+08:00",
			"osversion": buildUserAgent(self.userAgent),
			"u_aref": "undefined",
		}

		# 最多 3 次：验证码通过 / acw 刷新后可以重试
		for _ in range(3):
			params["_"] = str(int(time.time() * 1000))

			response = self.session.post(
				settings.FLIGHT_API,
				params=params,
				cookies=self.cookies,
				headers=self.headers,
				data=json.dumps(body, separators=(",", ":")),
				verify=False,
			)
			text = response.text

			# ---------- 1) 验证码 ----------
			if "请进行验证" in text or "别离开，为了更好的访问体验" in text:
				logger.info(f"<{date} {dep_city}->{arr_city}> 出现验证码")
				token = self._pick(text, r'"token":"(.*?)","traceid":')
				traceid = self._pick(text, r'"traceid":"(.*?)","type":"POST",')

				if not (token and traceid):
					raise CollectError("captcha_failed", "验证码页缺少 token/traceid")
				if not RiskApi.verifyCaptcha(traceid, self.userAgent, self.proxy):
					raise CollectError("captcha_failed", f"traceid={traceid}")

				# 通过后带上凭证重试
				params["u_atoken"] = token
				params["u_asig"] = traceid
				continue

			# ---------- 2) 访问被阻断 ----------
			if "您的访问被阻断" in text or "对网站造成安全威胁" in text:
				raise CollectError("ip_blocked", "访问被阻断（IP 或账号被风控）")

			# ---------- 3) 反爬参数过期 ----------
			if "acw_sc__v2" in text:
				arg = matchArg(text)
				if not arg:
					raise CollectError("acw_failed", "正则未匹配到 arg")
				acw = RiskApi.getAcw(arg)
				if not acw:
					raise CollectError("acw_failed", "风控服务未返回 acw_sc__v2")
				self.cookies["acw_sc__v2"] = acw
				continue

			# ---------- 4) 正常业务响应 ----------
			return self._parse_result(response, date, dep_city, arr_city)

		# 三次都没拿到有效响应（连续验证码 / 反爬挑战）
		raise CollectError("retry_exhausted", "3 次请求均未拿到有效响应")

	# ---------- 内部工具 ----------
	@staticmethod
	def _pick(text: str, pattern: str):
		found = re.findall(pattern, text)
		return found[0] if found else None

	@staticmethod
	def _parse_result(response, date, dep_city, arr_city) -> dict:
		"""把业务响应转成统一结构，业务错误码转成 CollectError"""
		try:
			payload = response.json()
		except Exception as e:
			# 返回的不是 JSON，通常是反爬页面的变种
			raise CollectError("api_error", f"响应非 JSON：{e}")

		code = payload.get("ERRORCODE")
		data_is_none = False

		if code:
			if code == "B20000":
				# 登录态失效，必须人工重新登录
				raise CollectError(
					"token_expired",
					"接口返回 B20000，请更新 info/token.txt",
				)
			if code == "S0004":
				# 该航线该日期没有可预订航班：正常空结果，仍要落库
				logger.warning(
					f"<{date} {dep_city}->{arr_city}> 无航班：{payload.get('MESSAGE')}"
				)
				data_is_none = True
			else:
				raise CollectError(
					"api_error",
					f"{code} {payload.get('MESSAGE') or ''}",
				)

		return {
			"json": payload,
			"http_status": response.status_code,
			"data_is_none": data_is_none,
		}
