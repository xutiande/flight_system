# -*- coding: utf-8 -*-



class CollectError(Exception):

	def __init__(self, reason: str, detail: str = ""):
		self.reason = reason
		self.detail = detail
		super().__init__(f"{reason}: {detail}" if detail else reason)


class AppError(Exception):
	"""系统级错误（配置缺失、依赖不可用等），与采集失败区分开"""
	pass


class InvalidParamError(AppError):
	"""
	入参不合法。

	CLI 捕获后打印提示，API 捕获后返回 400。
	不放在 CollectError 体系里，因为它跟「采集失败」不是一类问题。
	"""
	pass


class InvalidCityError(InvalidParamError):
	"""城市码 / 机场码无法识别"""
	pass


class NotFoundError(AppError):
	"""请求的资源不存在（如查询的航班没有记录），API 层映射为 404"""
	pass
