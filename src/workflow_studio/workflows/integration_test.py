"""Explicit real DeepSeek integration test; never collected by normal pytest."""

import argparse
from collections.abc import Sequence

from workflow_studio.state.workflow import WorkflowStatus
from workflow_studio.workflows.runner import run_workflow

INTEGRATION_REQUEST = "生成一个极简本地 Python 加法工具，add(a: int, b: int) 返回两个整数之和。只使用标准库，代码放 calculator.py，另提供一个 unittest 测试文件和简短运行说明，不使用数据库、网络访问或第三方依赖。"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit real multi-Agent workflow test (incurs DeepSeek API usage).")
    parser.add_argument("--run", action="store_true", help="Allow actual paid API calls")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("Pass --run to explicitly enable the real API integration test.")
    result = run_workflow(INTEGRATION_REQUEST)
    print(f"run_id={result.run_id} status={result.status}")
    for event in result.events:
        print(f"{event.agent_name}: {event.event_type} ({event.status})")
    if result.status != WorkflowStatus.COMPLETED:
        print(f"Integration test failed: {result.error}")
        return 1
    assert result.code is not None
    print(f"Integration test passed; generated {len(result.code.files)} files as text only.")
    print("Generated code and tests were not executed or exported.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
