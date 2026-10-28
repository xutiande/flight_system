# -*- coding: utf-8 -*-
import os
import sys

# 支持两种启动方式：
#   python -m csair.api.main      （推荐，__package__ 有值，cwd 已在 sys.path）
#   python csair/api/main.py      （此时 __package__ 为 None，需要手动补项目根）
if __name__ == "__main__" and __package__ is None:
	sys.path.insert(
		0,
		os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
	)

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from csair import __version__
from csair.api.routers import cities, flights, monitors, routes
from csair.api.schemas import HealthResponse
from csair.database import MySQLDB
from csair.exceptions import InvalidParamError, InvalidCityError, NotFoundError

# 前端静态资源目录：<项目根>/csair/web
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(
	title="南航航班数据接口",
	description=(
		"采集自南航官网的航班与舱位价格。\n\n"
		"- 价格一律是**含税总价**（price_total）\n"
		"- 城市码会自动展开成该城市所有机场\n"
		"- 数据每日 17:30 起增量采集"
	),
	version=__version__,
)

# 本地开发时前端可能跑在 5173 / 3000，放开跨域
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_methods=["GET"],
	allow_headers=["*"],
)


# ============================================================
# 异常 -> HTTP 状态码
# ============================================================

@app.exception_handler(InvalidCityError)
async def handle_invalid_city(request: Request, exc: InvalidCityError):
	"""城市码 / 机场码无法识别 -> 400"""
	return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(InvalidParamError)
async def handle_invalid_param(request: Request, exc: InvalidParamError):
	"""日期格式等入参问题 -> 400"""
	return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(NotFoundError)
async def handle_not_found(request: Request, exc: NotFoundError):
	"""查询的航班/资源不存在 -> 404"""
	return JSONResponse(status_code=404, content={"detail": str(exc)})


# ============================================================
# 路由
# ============================================================

app.include_router(flights.router)
app.include_router(routes.router)
app.include_router(cities.router)
app.include_router(monitors.router)

# 前端静态资源（样式、脚本）
if WEB_DIR.exists():
	app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/", include_in_schema=False)
def index():
	"""返回前端页面"""
	page = WEB_DIR / "index.html"
	if page.exists():
		return FileResponse(page)
	return JSONResponse(
		status_code=200,
		content={
			"message": "前端页面未找到，接口仍然可用",
			"docs": "/docs",
			"expected_file": str(page),
		},
	)


@app.get("/api", tags=["meta"], summary="服务信息")
def api_index():
	return {
		"name": "南航航班数据接口",
		"version": __version__,
		"docs": "/docs",
		"endpoints": [
			"GET /api/cities",
			"GET /api/flights/search",
			"GET /api/flights/trend",
			"GET /api/routes",
			"GET /health",
		],
	}


@app.get("/health", response_model=HealthResponse, tags=["meta"],
         summary="健康检查")
def health():
	"""给部署脚本和监控用。数据库连不上时返回 degraded"""
	try:
		db = MySQLDB()
		db.query_one("select 1 as ok")
		mysql = "ok"
	except Exception as e:
		mysql = f"error: {type(e).__name__}"

	return {
		"status": "ok" if mysql == "ok" else "degraded",
		"mysql": mysql,
		"version": __version__,
	}


# ============================================================
# 直接运行
# ============================================================

if __name__ == "__main__":
	import uvicorn

	from csair.settings import API

	print()
	print("=" * 56)
	print("  南航航班数据接口")
	print(f"  监听  http://{API['host']}:{API['port']}")
	print(f"  文档  http://{API['host']}:{API['port']}/docs")
	print("  停止  Ctrl+C")
	print("=" * 56)
	print()

	uvicorn.run(
		app,
		host=API["host"],
		port=API["port"],
		log_level="info",
	)
