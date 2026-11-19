import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from config import TIKHUB_BASE
from database import get_setting

_ctx = ssl.create_default_context()


class ApiError(Exception):
    pass


def request(method, path, params=None, body=None, retries=3, timeout=45):
    key = (get_setting("api_key") or "").strip()
    if not key:
        raise ApiError("还没有配置数据接口 Key：请管理员在「管理后台 → 设置」里填写（获取方式见 README）")
    url = TIKHUB_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {key}",
        # Cloudflare in front of TikHub rejects urllib's default User-Agent (error 1010).
        "User-Agent": "tikhub-python-sdk/2.1.1",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    last = ""
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, context=_ctx, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}"
            if e.code in (401, 402, 403, 404, 422):
                raise ApiError(last)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise ApiError(last)


def get(path, **params):
    return request("GET", path, params=params)


def post(path, **body):
    return request("POST", path, body={k: v for k, v in body.items() if v is not None})


def balance():
    try:
        return get("/api/v1/tikhub/user/get_user_info").get("user_data", {}).get("balance")
    except ApiError:
        return None
