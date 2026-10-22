"""Explicit paid API check, separate from pytest collection.

Run: python -X utf8 -m workflow_studio.llm.smoke_test --run
"""

import argparse
import logging
from collections.abc import Sequence

from langchain_core.prompts import ChatPromptTemplate

from workflow_studio.llm.deepseek_client import DeepSeekClient, get_llm
from workflow_studio.llm.errors import LLMCallError, LLMError

SMOKE_INPUT = "请只回答：DeepSeek API 连接成功。"


def run_smoke_test(client: DeepSeekClient) -> str:
    """Format one prompt and require nonempty text from one logical model call."""
    prompt = ChatPromptTemplate.from_messages([("human", "{request}")])
    message = client.invoke(prompt.invoke({"request": SMOKE_INPUT}))
    text = message.text.strip()
    if not text:
        raise LLMCallError("DeepSeek returned empty text; API smoke test failed.")
    return text


def main(argv: Sequence[str] | None = None) -> int:
    """Execute only with --run; never print raw SDK exceptions or credentials."""
    parser = argparse.ArgumentParser(description="Explicit DeepSeek API smoke test (may incur API charges).")
    parser.add_argument("--run", action="store_true", help="Explicitly allow the real API call")
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("Pass --run to explicitly execute the API smoke test.")

    # Configure only our logger; avoid enabling HTTP/SDK debug logging.
    call_logger = logging.getLogger("workflow_studio.llm.deepseek_client")
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level, previous_propagate = call_logger.level, call_logger.propagate
    call_logger.addHandler(handler)
    call_logger.setLevel(logging.INFO)
    call_logger.propagate = False
    try:
        text = run_smoke_test(get_llm())
    except LLMError as error:
        print(f"API smoke test failed [{type(error).__name__}]: {error}")
        return 1
    else:
        print(f"API smoke test passed: {text}")
        return 0
    finally:
        call_logger.removeHandler(handler)
        handler.close()
        call_logger.setLevel(previous_level)
        call_logger.propagate = previous_propagate


if __name__ == "__main__":
    raise SystemExit(main())
