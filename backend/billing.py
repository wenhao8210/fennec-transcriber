"""阿里云账户余额查询（BSS 费用中心 OpenAPI），供「每日录音」右侧的额度显示使用。

为什么不能用百炼的 API Key：
    百炼的 sk- Key 只能调模型（转写），查询费用/余额必须用阿里云 AccessKey。
    建议创建一个 RAM 子账号，只授予 `AliyunBSSReadOnlyAccess`（只读费用），
    不要直接把主账号 AccessKey 填进来。

口径：
    阿里云账单是 T+1，余额本身实时。这里按天缓存（CACHE_TTL_SEC），
    默认一天最多调一次，与「T+1 查余额」的预期一致；
    前端点一下可以传 force=True 强制刷新。

计费换算：
    fun-asr 华北2（北京）0.00022 元/秒 ≈ 0.792 元/小时，用它把余额折算成"还能转多久"。
"""
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

from aliyun_asr import load_settings, save_settings
from config import DATA_DIR

CACHE_PATH = DATA_DIR / "billing_cache.json"
CACHE_TTL_SEC = 24 * 3600                 # T+1：一天最多自动查一次
DEFAULT_ENDPOINT = "https://business.aliyuncs.com/"   # 中国站 BSS OpenAPI
BSS_VERSION = "2017-12-14"
FUN_ASR_YUAN_PER_HOUR = 0.792             # fun-asr 华北2(北京) 单价，用于折算可转时长


# ---------- AccessKey 存取（与百炼 Key 同一个 settings.json） ----------

def get_ak() -> tuple:
    s = load_settings()
    return (os.environ.get("ALIYUN_ACCESS_KEY_ID") or s.get("aliyun_ak_id") or "").strip(), (os.environ.get("ALIYUN_ACCESS_KEY_SECRET") or s.get("aliyun_ak_secret") or "").strip()


def set_ak(ak_id: str, ak_secret: str):
    data = load_settings()
    data["aliyun_ak_id"] = (ak_id or "").strip()
    data["aliyun_ak_secret"] = (ak_secret or "").strip()
    save_settings(data)


# ---------- BSS OpenAPI：QueryAccountBalance（RPC 签名 v1.0，纯标准库实现） ----------

def _pe(v) -> str:
    """阿里云要求的 percentEncode：quote 后 * -> %2A，~ 保留。"""
    return urllib.parse.quote(str(v), safe="")


def _sign(params: dict, secret: str) -> str:
    qs = "&".join(f"{_pe(k)}={_pe(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + _pe(qs)
    digest = hmac.new((secret + "&").encode("utf-8"),
                      string_to_sign.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("utf-8")


def query_balance(ak_id: str, ak_secret: str, timeout: int = 10) -> dict:
    """调用 QueryAccountBalance，返回 Data 字典。"""
    endpoint = (load_settings().get("aliyun_billing_endpoint") or "").strip() or DEFAULT_ENDPOINT
    params = {
        "Action": "QueryAccountBalance",
        "Format": "JSON",
        "Version": BSS_VERSION,
        "AccessKeyId": ak_id,
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": uuid.uuid4().hex,
        "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    params["Signature"] = _sign(params, ak_secret)
    url = endpoint.rstrip("/") + "/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            data = json.loads(f.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        code = msg = ""
        try:
            j = json.loads(raw)
            code = str(j.get("Code") or "")
            msg = str(j.get("Message") or "")
        except Exception:
            pass
        if not msg:
            msg = (raw[:200] or "无响应体")
        hint = {
            "InvalidAccessKeyId.NotFound": "（AccessKey ID 不存在，请核对；国际站账号需另配 endpoint）",
            "SignatureDoesNotMatch": "（AccessKey Secret 不正确）",
            "Forbidden": "（该 AccessKey 没有费用中心权限，请授予 AliyunBSSReadOnlyAccess）",
            "Forbidden.RAM": "（该 RAM 用户没有费用中心权限，请授予 AliyunBSSReadOnlyAccess）",
            "NoPermission": "（该 AccessKey 没有费用中心权限，请授予 AliyunBSSReadOnlyAccess）",
        }.get(code, "")
        raise RuntimeError(f"{code or ('HTTP ' + str(e.code))}: {msg}{hint}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误：{getattr(e, 'reason', e)}") from e
    except Exception as e:
        raise RuntimeError(f"响应解析失败：{e}") from e
    if data.get("Success") is False or str(data.get("Code") or "200") != "200":
        raise RuntimeError(f"{data.get('Code')}: {data.get('Message')}")
    return data.get("Data") or {}


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _shape(d: dict) -> dict:
    cash = _num(d.get("AvailableCashAmount"))
    credit = _num(d.get("CreditAmount"))
    avail = _num(d.get("AvailableAmount"))
    if not avail:
        avail = cash + credit
    return {
        "available": round(avail, 2),
        "cash": round(cash, 2),
        "credit": round(credit, 2),
        "currency": d.get("Currency") or "CNY",
        "hours_left": round(avail / FUN_ASR_YUAN_PER_HOUR, 1) if avail > 0 else 0.0,
    }


# ---------- 缓存 + 对外入口 ----------

def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(rec: dict):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_billing(force: bool = False) -> dict:
    """返回余额信息。

    configured=False 表示还没填 AccessKey；有 error 时若本地有上次结果会一并返回（stale）。
    """
    ak_id, ak_secret = get_ak()
    cache = _load_cache()
    if not (ak_id and ak_secret):
        out = {"configured": False, "cached": bool(cache), "error": ""}
        out.update({k: cache[k] for k in
                    ("available", "cash", "credit", "currency", "hours_left", "updated")
                    if k in cache})
        return out

    if cache and not force and time.time() - _num(cache.get("fetched_at")) < CACHE_TTL_SEC:
        return {**cache, "configured": True, "cached": True, "error": ""}

    try:
        rec = {
            **_shape(query_balance(ak_id, ak_secret)),
            "fetched_at": time.time(),
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "ak_tail": ak_id[-4:],
        }
    except Exception as e:
        if cache:
            return {**cache, "configured": True, "cached": True, "stale": True, "error": str(e)}
        return {"configured": True, "cached": False, "error": str(e)}

    _save_cache(rec)
    return {**rec, "configured": True, "cached": False, "error": ""}
