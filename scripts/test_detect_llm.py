"""测试 /workflow/detect 端点（use_llm=True）"""
import httpx
import json
import sys
import os

BASE_URL = "http://127.0.0.1:8000"

def test_detect_llm():
    """测试 Detect LLM 模式"""
    print("=" * 60)
    print("测试 POST /workflow/detect (use_llm=true)")
    print("=" * 60)

    data = {
        "customer_name": "桂平金山",
        "task_name": "bank_ledger_match",
        "use_llm": "true",
    }

    print(f"\n请求参数: {json.dumps(data, ensure_ascii=False, indent=2)}")
    print(f"\n发送请求...")

    try:
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                f"{BASE_URL}/workflow/detect",
                data=data,
            )
            resp.raise_for_status()
            result = resp.json()

            print(f"\n✅ 响应成功 (HTTP {resp.status_code})")
            print(f"\n--- 关键字段 ---")
            print(f"ok:           {result.get('ok')}")
            print(f"files_count:  {result.get('files_count')}")
            print(f"llm_used:     {result.get('llm_used')}")
            print(f"llm_error:    {result.get('llm_error')}")

            if result.get("llm_error"):
                print(f"\n❌ LLM 失败！错误详情:")
                err = result["llm_error"]
                print(f"  类型: {err.get('type')}")
                print(f"  消息: {err.get('message')}")
                print(f"  堆栈:\n{err.get('traceback', 'N/A')}")
            else:
                print(f"\n✅ LLM 调用成功！")

            print(f"\n--- 识别结果 ---")
            idents = result.get("identifications", [])
            for i, ident in enumerate(idents):
                print(f"  [{i+1}] {json.dumps(ident, ensure_ascii=False)}")

            print(f"\n--- task_yml_path ---")
            print(f"  {result.get('task_yml_path', 'N/A')}")

            return result

    except httpx.HTTPStatusError as e:
        print(f"\n❌ HTTP 错误 {e.response.status_code}: {e.response.text}")
    except httpx.ConnectError:
        print(f"\n❌ 无法连接到后端 {BASE_URL}，请确认后端已启动")
    except Exception as e:
        print(f"\n❌ 异常: {type(e).__name__}: {e}")

    return None


if __name__ == "__main__":
    test_detect_llm()