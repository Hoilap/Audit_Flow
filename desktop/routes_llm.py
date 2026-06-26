"""desktop.routes_llm — LLM 代码生成端点 (generate / generate_and_run)。"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from fastapi import APIRouter, Form

from .common import (
    logger,
    token_tracker,
    notify_frontend,
    _default_llm_config,
    _build_llm_target_path,
)
from audit_workflow.llm_agent import llm_generate as audit_llm_generate

router = APIRouter()


def _strip_code_fences(text: str) -> str:
    """剥离 LLM 输出中的 markdown 代码块标记，提取纯代码。"""
    # 优先匹配 ```python ... ```
    m = re.search(r"```python\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    # 退而匹配 ``` ... ```
    m = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text.strip()


def _build_retry_prompt(
    original: str,
    generated_code: str,
    error_type: str,
    error_detail: str,
) -> str:
    """构造重试提示词模板，引导 LLM 修复上一次生成的代码。"""
    # 截断过长的代码，避免 token 爆炸
    code_block = generated_code
    if len(code_block) > 4000:
        code_block = code_block[:4000] + "\n... (truncated)"
    return (
        f"你上一次生成的 Python 代码存在{error_type}，请修复后重新输出完整代码。\n"
        f"直接输出纯 Python 代码，不要用 ``` 代码块包裹，不要任何额外解释。\n\n"
        f"## {error_type}\n```\n{error_detail}\n```\n\n"
        f"## 上次生成的代码\n```python\n{code_block}\n```\n\n"
        f"## 原始需求\n{original}"
    )


def _tail_lines(text: str, max_lines: int) -> str:
    """保留文本末尾最多 max_lines 行，前面用 ... 标记截断。"""
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    return "... (truncated)\n" + "\n".join(lines[-max_lines:])


@router.post("/llm/generate")
def llm_generate(
    prompt: str = Form(...),
    target_path: str = Form(""),
    system_prompt: str = Form(""),
    customer_name: str = Form(""),
    task_name: str = Form(""),
):
    if not target_path:
        target_path = _build_llm_target_path(customer_name, task_name, ext=".txt")
    logger.info("LLM 生成请求: target=%s, prompt_len=%d, system_prompt_len=%d",
                target_path, len(prompt), len(system_prompt))
    usage_info = {}
    try:
        content, usage_info = audit_llm_generate(
            _default_llm_config(), prompt,
            system_prompt=system_prompt or "",
        )
        token_tracker.record(usage_info)
        logger.info("LLM 生成完成: tokens=%s", usage_info.get("total_tokens", 0))
    except RuntimeError as e:
        content = f"[LLM not configured] {e}\nPrompt received:\n{prompt}"

    if target_path.endswith('.py'):
        content = _strip_code_fences(content)

    p = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return {"path": os.path.relpath(p), "ok": True, "usage": usage_info}


@router.post("/llm/generate_and_run")
def llm_generate_and_run(
    prompt: str = Form(...),
    target_path: str = Form(""),
    run_code: str = Form("false"),
    timeout: int = Form(5),
    system_prompt: str = Form(""),
    customer_name: str = Form(""),
    task_name: str = Form(""),
):
    import ast
    if not target_path:
        target_path = _build_llm_target_path(customer_name, task_name, ext=".py")
    logger.info("LLM 生成并执行: target=%s, run=%s, timeout=%ds, system_prompt_len=%d",
                target_path, run_code, timeout, len(system_prompt))
    run_flag = str(run_code).lower() in ("1", "true", "yes", "on")
    need_check = target_path.endswith('.py') and run_flag

    max_retries = 3
    content = ""
    run_result = None
    last_error: str | None = None       # 最终错误描述（语法 or 运行）
    last_error_type: str | None = None  # "syntax" | "runtime" | None
    total_usage: dict = {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
    current_prompt = prompt

    for attempt in range(max_retries + 1):
        # ── 1. LLM 生成 ──
        try:
            content, usage_info = audit_llm_generate(
                _default_llm_config(), current_prompt,
                system_prompt=system_prompt or "",
            )
            token_tracker.record(usage_info)
            for k in total_usage:
                total_usage[k] += usage_info.get(k, 0)
            logger.info("LLM 生成完成: tokens=%s (attempt %d/%d)",
                        usage_info.get("total_tokens", 0), attempt + 1, max_retries + 1)
        except RuntimeError as e:
            if run_flag and target_path.endswith('.py'):
                content = prompt
            else:
                content = f"[LLM not configured] {e}\nPrompt received:\n{prompt}"
            break

        # ── 1.5 剥离 markdown 代码块标记 ──
        if target_path.endswith('.py'):
            content = _strip_code_fences(content)

        # ── 2. 保存文件（每次都保存，包括出错的版本）──
        p = os.path.abspath(target_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)

        if not need_check:
            break  # 无需校验/执行，直接结束

        # ── 3. 语法检查 ──
        try:
            ast.parse(content)
        except SyntaxError as e:
            last_error = str(e)
            last_error_type = "syntax"
            logger.warning("LLM 代码语法错误 (attempt %d/%d): %s",
                           attempt + 1, max_retries + 1, e)
            if attempt < max_retries:
                logger.info("自动重试修复语法错误 (attempt %d → %d)", attempt + 1, attempt + 2)
                notify_frontend("retry", {
                    "reason": "syntax",
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "error": str(e),
                })
                current_prompt = _build_retry_prompt(
                    original=prompt,
                    generated_code=content,
                    error_type="语法错误",
                    error_detail=str(e),
                )
                continue
            break  # 重试耗尽

        # ── 4. 执行代码 ──
        run_result = None
        exec_error_msg = None
        try:
            proc = subprocess.run(
                [sys.executable, p], capture_output=True, text=True, timeout=timeout,
            )
            run_result = {
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            }
            logger.info("LLM 代码执行完成: returncode=%d", proc.returncode)
            if proc.returncode != 0:
                # 截取关键错误信息，避免 prompt 过长
                stderr_snippet = (proc.stderr or "").strip()
                stdout_snippet = (proc.stdout or "").strip()
                parts = [f"returncode={proc.returncode}"]
                if stderr_snippet:
                    parts.append(f"stderr:\n{_tail_lines(stderr_snippet, 30)}")
                if stdout_snippet and not stderr_snippet:
                    parts.append(f"stdout:\n{_tail_lines(stdout_snippet, 30)}")
                exec_error_msg = "\n".join(parts)
        except subprocess.TimeoutExpired:
            run_result = {"error": "timeout"}
            exec_error_msg = f"执行超时（>{timeout}s）"
            logger.warning("LLM 代码执行超时: %s", target_path)

        if exec_error_msg is None:
            # 执行成功
            last_error = None
            last_error_type = None
            break
        else:
            last_error = exec_error_msg
            last_error_type = "runtime"
            logger.warning("LLM 代码运行错误 (attempt %d/%d): %s",
                           attempt + 1, max_retries + 1, exec_error_msg)
            if attempt < max_retries:
                logger.info("自动重试修复运行错误 (attempt %d → %d)", attempt + 1, attempt + 2)
                notify_frontend("retry", {
                    "reason": "runtime",
                    "attempt": attempt + 1,
                    "max_retries": max_retries,
                    "error": exec_error_msg[:200],
                })
                current_prompt = _build_retry_prompt(
                    original=prompt,
                    generated_code=content,
                    error_type="运行错误",
                    error_detail=exec_error_msg,
                )
                continue
            break  # 重试耗尽

    # ── 5. 最终保存（确保最后一次生成的代码落盘）──
    p = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)

    # 如果循环中已经执行过且成功，复用之前的 run_result；否则置 None
    if last_error is not None:
        run_result = None

    resp: dict = {
        "path": os.path.relpath(p),
        "ok": last_error is None,
        "run_result": run_result,
        "usage": total_usage,
        "retries": attempt,
    }
    if last_error:
        resp["error_type"] = last_error_type
        resp["error_detail"] = last_error
    return resp
