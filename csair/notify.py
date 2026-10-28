# -*- coding: utf-8 -*-

import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests
from loguru import logger

from csair.settings import MAIL_DEFAULTS, MAIL_FILE


class NotifyError(Exception):
	"""通知发送失败，原因会写进 price_alert.error_msg"""
	pass


# ============================================================
# 邮件配置
# ============================================================

_MAIL_CACHE = {"data": None, "mtime": None}


def load_mail_config() -> dict:
	"""
	读 info/mail.txt（key=value 格式），和默认值合并。

	文件改动后自动重新加载（按修改时间判断），改完不用重启。
	"""
	cfg = dict(MAIL_DEFAULTS)

	if not MAIL_FILE.exists():
		return cfg

	try:
		mtime = MAIL_FILE.stat().st_mtime
	except OSError:
		return cfg

	if _MAIL_CACHE["data"] is not None and _MAIL_CACHE["mtime"] == mtime:
		return _MAIL_CACHE["data"]

	for raw in MAIL_FILE.read_text(encoding="utf-8").splitlines():
		line = raw.strip()
		if not line or line.startswith("#") or "=" not in line:
			continue
		key, _, value = line.partition("=")
		key = key.strip()
		if key in cfg:
			cfg[key] = value.strip()

	cfg["smtp_port"] = int(cfg.get("smtp_port") or 465)

	_MAIL_CACHE["data"] = cfg
	_MAIL_CACHE["mtime"] = mtime
	return cfg


def mail_ready() -> bool:
	"""发件账号和授权码都填了才算配好"""
	cfg = load_mail_config()
	return bool(cfg["user"] and cfg["auth_code"])


# ============================================================
# 邮件
# ============================================================

def send_email(subject: str, html: str, to: str = None) -> str:
	"""
	发一封 HTML 邮件，返回实际收件人。

	:raises NotifyError: 配置缺失 / 登录失败 / 网络异常
	"""
	cfg = load_mail_config()

	if not cfg["user"] or not cfg["auth_code"]:
		raise NotifyError(
			f"邮件未配置：请在 {MAIL_FILE} 里填写 user（发件邮箱）"
		)

	sender = cfg["user"].strip()
	recipient = (to or cfg["default_to"] or sender).strip()

	msg = MIMEText(html, "html", "utf-8")
	msg["Subject"] = Header(subject, "utf-8")
	msg["From"] = formataddr((str(Header(cfg["from_name"], "utf-8")), sender))
	msg["To"] = recipient

	try:
		# QQ 邮箱用 SSL（465），不用 STARTTLS
		with smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=15) as s:
			s.login(sender, cfg["auth_code"])
			s.sendmail(sender, [recipient], msg.as_string())
	except smtplib.SMTPAuthenticationError as e:
		raise NotifyError(f"SMTP 认证失败（授权码可能失效）：{e}")
	except Exception as e:
		raise NotifyError(f"发送失败：{type(e).__name__}: {e}")

	logger.success(f"[通知] 邮件已发送 → {recipient}")
	return recipient


# ============================================================
# Webhook
# ============================================================

def send_webhook(url: str, payload: dict) -> None:
	"""企业微信 / 钉钉 / 自定义接收端。失败抛 NotifyError"""
	if not url:
		raise NotifyError("Webhook 地址为空")

	try:
		resp = requests.post(url, json=payload, timeout=10)
	except Exception as e:
		raise NotifyError(f"Webhook 请求失败：{type(e).__name__}: {e}")

	if resp.status_code >= 400:
		raise NotifyError(f"Webhook 返回 {resp.status_code}：{resp.text[:200]}")

	logger.success(f"[通知] Webhook 已发送 → {url[:40]}...")


# ============================================================
# 邮件正文
# ============================================================

def route_label(rule: dict) -> str:
	"""
	航线显示名：长沙(CSX) → 北京(BJS)

	只写「长沙 → 北京」时，用户看到别处的三字码会对不上；
	只写「CSX → BJS」又不知道是哪儿。两个都给。
	"""
	dep_name = rule.get("dep_name") or rule.get("dep_city") or ""
	arr_name = rule.get("arr_name") or rule.get("arr_city") or ""
	dep_code = rule.get("dep_city") or ""
	arr_code = rule.get("arr_city") or ""

	left = f"{dep_name}({dep_code})" if dep_code else dep_name
	right = f"{arr_name}({arr_code})" if arr_code else arr_name
	return f"{left} → {right}"


def render_alert_email(rule: dict, offer: dict) -> tuple:
	"""
	生成 (subject, html)。

	标题要能一眼看出结论：哪个航线、多少钱、低于阈值多少。
	"""
	route = route_label(rule)
	subject = (
		f"[降价] {route} {rule['target_date']} "
		f"¥{offer['price_total']:.0f}（阈值 ¥{rule['max_price']:.0f}）"
	)

	saving = float(rule["max_price"]) - float(offer["price_total"])
	html = f"""
	<div style="font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;
	            max-width:560px;margin:0 auto;padding:24px;color:#16181d">
	  <h2 style="margin:0 0 4px;font-size:18px">✈️ 机票降价提醒</h2>
	  <p style="margin:0 0 20px;color:#8b93a1;font-size:13px">
	    {route} · {rule['target_date']}
	  </p>

	  <div style="background:#fdeef1;border-radius:12px;padding:18px 20px;margin-bottom:20px">
	    <div style="font-size:13px;color:#6b7280">当前最低含税价</div>
	    <div style="font-size:34px;font-weight:700;color:#c8102e;line-height:1.2">
	      ¥{offer['price_total']:.0f}
	    </div>
	    <div style="font-size:13px;color:#6b7280;margin-top:6px">
	      你设定的阈值 ¥{rule['max_price']:.0f}，
	      便宜了 <b style="color:#c8102e">¥{saving:.0f}</b>
	    </div>
	  </div>

	  <table style="width:100%;border-collapse:collapse;font-size:13px">
	    <tr><td style="padding:8px 0;color:#8b93a1;width:96px">航班</td>
	        <td style="padding:8px 0;font-weight:600">{offer['flight_no']}</td></tr>
	    <tr><td style="padding:8px 0;color:#8b93a1">时间</td>
	        <td style="padding:8px 0">{offer['dep_time']} → {offer['arr_time']}</td></tr>
	    <tr><td style="padding:8px 0;color:#8b93a1">舱位</td>
	        <td style="padding:8px 0">{offer['cabin_code']} 舱
	          {('（' + offer['cabin_class'] + '）') if offer.get('cabin_class') else ''}</td></tr>
	    <tr><td style="padding:8px 0;color:#8b93a1">采集时间</td>
	        <td style="padding:8px 0">{offer.get('captured_at') or '—'}</td></tr>
	  </table>

	  <p style="margin:20px 0 0;padding-top:16px;border-top:1px solid #e6e8ec;
	            font-size:12px;color:#8b93a1">
	    价格以南方航空官网为准。此邮件由采集系统自动发送，同一规则 24 小时内最多一封。
	  </p>
	</div>
	"""
	return subject, html
