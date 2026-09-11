"""测试天眼查开放平台 API 调用（企业基本信息接口）。

零依赖，仅用 Python 标准库。

## Token 申请指引
1. 打开天眼查开放平台 https://open.tianyancha.com/ 注册账号并完成企业/个人实名认证。
2. 进入「控制台」创建应用，获取 API Token（不同套餐有不同接口权限与次数配额）。
3. 企业基本信息接口需开通「企业基本信息」相关权限（标准版/专业版套餐）。
4. 拿到 token 后二选一配置：
   - 环境变量: set TYC_TOKEN=你的token   (Windows) 或 export TYC_TOKEN=... (macOS/Linux)
   - 命令行参数: python scripts/test_tianyancha.py "公司名" --token 你的token
   也可以写在项目根目录 .env 文件里（TYC_TOKEN=你的token，脚本会自动读取）。

## 用法
    python scripts/test_tianyancha.py 阿里巴巴
    python scripts/test_tianyancha.py 91330110MA2B1FLE8Q          # 统一社会信用代码
    python scripts/test_tianyancha.py 阿里巴巴 --dry-run          # 只打印请求内容，不发网络请求（无需 token）
    python scripts/test_tianyancha.py 阿里巴巴 --token 8b0380cd-8cea-454e-884a-1584986fd4de --url https://open.api.tianyancha.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Windows 控制台默认 GBK，强制 stdout 用 UTF-8 输出，避免中文乱码
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 企业基本信息接口（normal 版，返回工商基础字段）
API_PATH = "/services/open/ic/baseinfo/normal"
DEFAULT_BASE_URL = "https://open.api.tianyancha.com"

# 天眼查开放平台常见业务错误码
ERROR_CODES = {
    300: "暂无数据（接口调用成功但未查到企业信息）",
    301: "调用次数或套餐余额不足",
    302: "未授权 / 当前套餐无此接口权限（需在开放平台开通「企业基本信息」）",
    303: "请求参数错误",
    400: "参数缺失或格式错误",
    404: "接口不存在（检查 URL 是否拼写正确）",
    300009: "账号信息有误（token 无效或已过期，请到开放平台控制台核对）",
    300010: "登录已过期（token 失效，请重新获取）",
}


def load_token_from_env_file(root: Path) -> str | None:
    """从项目根目录 .env 读取 TYC_TOKEN（简单解析，不依赖 python-dotenv）。"""
    env_path = root / ".env"
    if not env_path.exists():
        return None
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "TYC_TOKEN":
                return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def resolve_token(args_token: str | None, root: Path) -> str | None:
    """按优先级取 token：命令行参数 > 环境变量 > 项目根目录 .env。"""
    return args_token or os.getenv("TYC_TOKEN") or load_token_from_env_file(root)


def build_request(base_url: str, token: str | None, keyword: str) -> urllib.request.Request:
    """构造 GET 请求。天眼查鉴权方式：HTTP 头 Authorization 携带 token。"""
    query = urllib.parse.urlencode({"keyword": keyword})
    url = f"{base_url.rstrip('/')}{API_PATH}?{query}"
    headers = {"User-Agent": "qoderwork-tyc-test/1.0"}
    if token:
        headers["Authorization"] = token
    return urllib.request.Request(url, headers=headers)


def print_summary(result: dict) -> None:
    """打印企业基本信息中的关键字段。"""
    keys = [
        ("name", "企业名称"),
        ("creditCode", "统一社会信用代码"),
        ("legalPersonName", "法定代表人"),
        ("regCapital", "注册资本"),
        ("regStatus", "经营状态"),
        ("estiblishTime", "成立时间"),
        ("regInstitute", "登记机关"),
        ("regLocation", "注册地址"),
        ("industry", "所属行业"),
        ("businessScope", "经营范围"),
    ]
    for key, label in keys:
        value = result.get(key)
        if key == "estiblishTime" and value:
            # 天眼查返回毫秒级 Unix 时间戳
            value = time.strftime("%Y-%m-%d", time.localtime(int(value) / 1000))
        if value:
            text = str(value)
            if len(text) > 80:
                text = text[:77] + "..."
            print(f"  {label}: {text}")


def call_api(base_url: str, token: str, keyword: str) -> dict:
    """发起真实调用，返回解析后的 JSON；失败时抛 SystemExit。"""
    req = build_request(base_url, token, keyword)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(f"HTTP {e.code} 错误：{body[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"网络错误：{e.reason}（请检查网络或代理设置）")
    except json.JSONDecodeError as e:
        raise SystemExit(f"返回内容不是合法 JSON：{e}")


def main() -> None:
    parser = argparse.ArgumentParser(description="测试天眼查开放平台企业基本信息接口")
    parser.add_argument("keyword", help="公司名称或统一社会信用代码")
    parser.add_argument("--token", help="API Token（默认读环境变量 TYC_TOKEN 或根目录 .env）")
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="接口基础 URL（默认 https://open.api.tianyancha.com）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要发送的请求，不发起网络调用")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    token = resolve_token(args.token, root)

    print("=" * 60)
    print("天眼查开放平台 · 企业基本信息接口测试")
    print("=" * 60)

    req = build_request(args.url, token, args.keyword)
    print(f"请求方法: GET")
    print(f"请求 URL : {req.full_url}")
    print(f"鉴权方式: Authorization 请求头" + (f" = {token[:8]}...（已隐藏）" if token else "（未提供）"))
    print()

    if args.dry_run:
        print("[dry-run] 未发起网络请求。确认以上请求内容无误后，去掉 --dry-run 并配置 token 即可真实调用。")
        return

    if not token:
        raise SystemExit(
            "未找到 token。请先在 https://open.tianyancha.com 申请，然后通过 "
            "--token 参数、环境变量 TYC_TOKEN 或根目录 .env 提供。\n"
            "想先验证请求格式可以加 --dry-run。"
        )

    print(f"查询关键词: {args.keyword}")
    print(f"正在调用接口...")
    data = call_api(args.url, token, args.keyword)

    error_code = data.get("error_code")
    reason = data.get("reason", "")

    if error_code == 0:
        print(f"调用成功（error_code=0, reason={reason}）\n")
        print("关键信息:")
        print_summary(data.get("result") or {})
        print("\n完整返回（截断前 2000 字符）:")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    else:
        hint = ERROR_CODES.get(error_code, "")
        print(f"调用失败: error_code={error_code}, reason={reason}")
        if hint:
            print(f"提示: {hint}")
        sys.exit(2)


if __name__ == "__main__":
    main()
