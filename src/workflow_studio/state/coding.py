"""Bounded, validated code artifacts; validation never executes generated code."""

import ast
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from workflow_studio.exporters.paths import validate_export_path

MAX_FILES = 20
MAX_FILE_BYTES = 64 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
CodeText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def utf8_size(text: str) -> int:
    """Measure byte limits consistently and reject invalid Unicode surrogates."""
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("output_must_be_valid_utf8") from None


class CodeFile(BaseModel):
    """One complete file stored only as text, with a safe relative target path."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    path: str = Field(min_length=1, max_length=240, description="使用正斜杠的安全相对文件路径，禁止绝对路径、路径穿越和敏感配置路径。")
    language: CodeText = Field(description="代码或文档语言，例如 python、json、markdown。")
    content: str = Field(min_length=1, max_length=MAX_FILE_BYTES, description="完整原始文件内容，保留缩进和换行；不得为空、截断或包含外层 Markdown 围栏。")
    description: CodeText = Field(description="文件用途及其对应模块、需求的说明。")

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return validate_export_path(value)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("file_content_must_not_be_blank")
        if utf8_size(value) > MAX_FILE_BYTES:
            raise ValueError("file_content_exceeds_byte_limit")
        return value

    @model_validator(mode="after")
    def validate_python_syntax(self) -> Self:
        if self.path.casefold().endswith(".py"):
            try:
                ast.parse(self.content, filename="<generated-python>")
            except (SyntaxError, ValueError, RecursionError):
                raise ValueError("generated_python_has_invalid_syntax") from None
        return self


class CodingOutput(BaseModel):
    """Complete multi-file output with bounded size and collision-free targets."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    files: list[CodeFile] = Field(min_length=1, max_length=MAX_FILES, description="生成的完整文件列表，最多 20 个；简单 Python 项目应包含基本测试。")
    instructions: CodeText = Field(description="环境准备、运行入口和手动验证说明；只提供文字，不自动执行命令。")
    dependencies: list[CodeText] = Field(description="运行或测试所需第三方依赖；仅标准库时可为空，不自动安装。")
    test_suggestions: list[CodeText] = Field(description="与验收标准关联的测试建议，说明尚未实际执行的测试。")
    limitations: list[CodeText] = Field(description="未实现范围、待确认假设和静态验证局限，不得谎称已运行成功。")

    @model_validator(mode="after")
    def validate_artifacts(self) -> Self:
        targets = [file.path.casefold() for file in self.files]
        if len(targets) != len(set(targets)):
            raise ValueError("duplicate_file_target")
        target_set = set(targets)
        for target in targets:
            parts = target.split("/")
            if any("/".join(parts[:i]) in target_set for i in range(1, len(parts))):
                raise ValueError("file_directory_target_conflict")
        if utf8_size(self.model_dump_json()) > MAX_OUTPUT_BYTES:
            raise ValueError("coding_output_exceeds_total_byte_limit")
        return self
