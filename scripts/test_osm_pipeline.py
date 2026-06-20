"""测试 outbound_settlement_match 全流程 API 端点。

用法:
    1. 启动后端: python -m desktop.api
    2. 运行测试: python scripts/test_osm_pipeline.py

可通过命令行参数指定客户名和任务名:
    python scripts/test_osm_pipeline.py --customer ABC --task outbound_settlement_match
"""
import httpx
import json
import sys
import time

BASE_URL = "http://127.0.0.1:8000"

# 默认测试参数
DEFAULT_CUSTOMER = "ABC"
DEFAULT_TASK = "outbound_settlement_match"


def _post(endpoint: str, data: dict, timeout: float = 300.0) -> dict | None:
    """发送 POST 请求并打印结果摘要。"""
    url = f"{BASE_URL}{endpoint}"
    print(f"\n{'='*60}")
    print(f"POST {endpoint}")
    print(f"{'='*60}")
    print(f"参数: {json.dumps(data, ensure_ascii=False)}")

    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, data=data)
            elapsed = time.time() - t0
            resp.raise_for_status()
            result = resp.json()

            ok = result.get("ok", "N/A")
            print(f"✅ 成功 (HTTP {resp.status_code}, {elapsed:.1f}s, ok={ok})")
            return result

    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP {e.response.status_code}: {e.response.text[:500]}")
    except httpx.ConnectError:
        print(f"❌ 无法连接后端 {BASE_URL}，请先启动: python -m desktop.api")
    except Exception as e:
        print(f"❌ 异常: {type(e).__name__}: {e}")
    return None


def test_detect(customer: str, task: str):
    """Step 1: 扫描文件、分类工作表。"""
    result = _post("/workflow/outbound_settlement_match/detect", {
        "customer_name": customer,
        "task_name": task,
    })
    if result and result.get("ok"):
        settle_count = result.get("settlement_files", 0)
        if isinstance(settle_count, list):
            settle_count = len(settle_count)
        outbound_count = result.get("outbound_files", 0)
        if isinstance(outbound_count, list):
            outbound_count = len(outbound_count)
        print(f"  结算文件: {settle_count} 个")
        print(f"  出库文件: {outbound_count} 个")
        # 打印工作表分类
        for f in result.get("outbound_files", []):
            sheets = f.get("sheets", [])
            types = [s["type"] for s in sheets]
            print(f"  {f['name']}: {types}")


def test_clean_settlement(customer: str, task: str):
    """Step 2: 清洗结算流水。"""
    result = _post("/workflow/outbound_settlement_match/clean_settlement", {
        "customer_name": customer,
        "task_name": task,
    })
    if result and result.get("ok"):
        print(f"  结算CSV: {result.get('settlement_csv')}")
        print(f"  月度汇总: {result.get('monthly_summary')}")


def test_clean_outbound(customer: str, task: str):
    """Step 3: 清洗出库报告。"""
    result = _post("/workflow/outbound_settlement_match/clean_outbound", {
        "customer_name": customer,
        "task_name": task,
    })
    if result and result.get("ok"):
        paths = result.get("paths", {})
        for name, path in paths.items():
            print(f"  {name}: {path or '(无数据)'}")


def test_match(customer: str, task: str):
    """Step 4: 净出库过滤 + ID 匹配。"""
    result = _post("/workflow/outbound_settlement_match/match", {
        "customer_name": customer,
        "task_name": task,
    }, timeout=600.0)
    if result and result.get("ok"):
        summary = result.get("summary", {})
        print(f"\n  --- 匹配统计 ---")
        for k, v in summary.items():
            print(f"  {k}: {v}")

        for key in ("net_outbound", "matched", "unmatched_outbound", "unmatched_settlement"):
            info = result.get(key, {})
            print(f"  {key}: {info.get('rows', 0)} 行 → {info.get('path', 'N/A')}")


def test_run_all(customer: str, task: str):
    """全流程一键测试 (Detect → Clean → Match)。"""
    result = _post("/workflow/outbound_settlement_match/run_all", {
        "customer_name": customer,
        "task_name": task,
    }, timeout=600.0)
    if result and result.get("ok"):
        summary = result.get("summary", {})
        detect = result.get("detect", {})
        print(f"  检测: {detect.get('settlement_files', 0)} 结算 + {detect.get('outbound_files', 0)} 出库")
        print(f"  匹配率: {summary.get('match_rate', 'N/A')}%")
        print(f"  匹配: {summary.get('matched_count', 0)}, 未匹配出库: {summary.get('unmatched_outbound_count', 0)}")


if __name__ == "__main__":
    # 解析命令行参数
    customer = DEFAULT_CUSTOMER
    task = DEFAULT_TASK
    mode = "all"  # all | step

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--customer" and i + 1 < len(args):
            customer = args[i + 1]
            i += 2
        elif args[i] == "--task" and i + 1 < len(args):
            task = args[i + 1]
            i += 2
        elif args[i] in ("detect", "clean_settlement", "clean_outbound", "match", "run_all"):
            mode = args[i]
            i += 1
        else:
            i += 1

    print(f"测试 outbound_settlement_match API")
    print(f"  客户: {customer}")
    print(f"  任务: {task}")
    print(f"  后端: {BASE_URL}")

    if mode == "all":
        test_detect(customer, task)
        test_clean_settlement(customer, task)
        test_clean_outbound(customer, task)
        test_match(customer, task)
    elif mode == "detect":
        test_detect(customer, task)
    elif mode == "clean_settlement":
        test_clean_settlement(customer, task)
    elif mode == "clean_outbound":
        test_clean_outbound(customer, task)
    elif mode == "match":
        test_match(customer, task)
    elif mode == "run_all":
        test_run_all(customer, task)
    else:
        print(f"未知模式: {mode}")
