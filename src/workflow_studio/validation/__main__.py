"""python -m workflow_studio.validation --list | --evaluate DIR --run."""

import argparse
import json
from pathlib import Path

from workflow_studio.validation.evaluation import evaluate_candidates
from workflow_studio.validation.suites import SUITES, SUITE_VERSION


def main():
    parser = argparse.ArgumentParser(description="固定 Python CLI 评测；仅 --run 显式启用容器执行，不调用模型。")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--evaluate", type=Path)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.list or not args.run:
        print(json.dumps({"dataset_version": SUITE_VERSION, "execution_enabled": False,
                          "tasks": [{"id": suite.id, "contract": suite.contract, "cases": len(suite.cases), "manual": suite.manual,
                                     "digest": suite.digest, "required_files": suite.required_files} for suite in SUITES]}, ensure_ascii=False, indent=2))
        return 0
    if args.evaluate is None:
        parser.error("--run 需要 --evaluate DIR")
    result = evaluate_candidates(args.evaluate)
    print(json.dumps({**result.model_dump(mode="json"), "metrics": result.metrics}, ensure_ascii=False, indent=2))
    return 0 if result.metrics["passed"] == sum(not suite.manual for suite in SUITES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
