"""Static validation only: generated artifacts are never executed or exported."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from workflow_studio.exporters.paths import validate_export_path
from workflow_studio.state.coding import MAX_FILES, MAX_FILE_BYTES, MAX_OUTPUT_BYTES, CodeFile, CodingOutput


def artifact(path: str = "main.py", content: str = "pass\n") -> CodeFile:
    return CodeFile(path=path, language="python", content=content, description="Application module")


def bundle(files: list[CodeFile], instructions: str = "Run manually") -> CodingOutput:
    return CodingOutput(files=files, instructions=instructions, dependencies=[], test_suggestions=["Run tests manually"], limitations=["Not executed"])


@pytest.mark.parametrize("path", [
    "/etc/passwd", "C:/temp/main.py", "C:main.py", "//server/share/file.py",
    "../main.py", "src/../../main.py", "src/../main.py", "src/./main.py",
    "src//main.py", "src/", "src\\main.py", "main.py:stream",
    "main.py ", "main.py.", "CON", "nul.txt", "COM1.py", "con .txt",
    ".git/config", ".env", "src/.env.local", ".ssh/config", ".github/workflows/run.yml",
    ".venv/main.py", ".streamlit/secrets.toml", "bad\x00.py", "bad\n.py",
    "a?.py", "%2e%2e/main.py", "Ａ.py", "a" * 241,
])
def test_unsafe_paths_rejected(path: str) -> None:
    with pytest.raises(ValueError):
        validate_export_path(path)
    with pytest.raises(ValidationError):
        artifact(path)


@pytest.mark.parametrize("path", ["src/main.py", "tests/test_main.py", "README.md", ".env.example", "src/工具.py"])
def test_safe_paths(path: str) -> None:
    assert validate_export_path(path) == path
    assert artifact(path).path == path


@pytest.mark.parametrize("content", ["", " \n\t", "def broken(", "if True:\n"])
def test_empty_or_invalid_python(content: str) -> None:
    with pytest.raises(ValidationError):
        artifact(content=content)


def test_exact_content_is_preserved() -> None:
    content = "# generated module\n\ndef answer() -> int:\n    return 42\n"
    assert artifact(content=content).content == content


def test_generated_code_is_never_executed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    code = "from pathlib import Path\nPath('must-not-exist.txt').write_text('executed')\nraise RuntimeError('never execute')\n"
    assert bundle([artifact(content=code)]).files[0].content == code
    assert not (tmp_path / "must-not-exist.txt").exists()
    assert not (tmp_path / "main.py").exists()


@pytest.mark.parametrize("paths", [["main.py", "MAIN.py"], ["src/app.py", "SRC/App.py"], ["a.txt", "a.txt/child.py"]])
def test_duplicate_or_conflicting_targets(paths: list[str]) -> None:
    with pytest.raises(ValidationError):
        bundle([artifact(path) for path in paths])


def test_same_basename_in_different_directories_is_valid() -> None:
    assert len(bundle([artifact("app/__init__.py"), artifact("tests/__init__.py")]).files) == 2


def test_file_count_limit() -> None:
    assert len(bundle([artifact(f"f{i}.py") for i in range(MAX_FILES)]).files) == MAX_FILES
    with pytest.raises(ValidationError):
        bundle([artifact(f"f{i}.py") for i in range(MAX_FILES + 1)])


def test_per_file_byte_limit() -> None:
    assert len(artifact("data.txt", "a" * MAX_FILE_BYTES).content) == MAX_FILE_BYTES
    with pytest.raises(ValidationError):
        artifact("data.txt", "a" * (MAX_FILE_BYTES + 1))
    with pytest.raises(ValidationError):
        artifact("data.txt", "中" * (MAX_FILE_BYTES // 3 + 1))


def test_total_output_includes_metadata() -> None:
    files = [artifact(f"data{i}.txt", "a" * MAX_FILE_BYTES) for i in range(4)]
    with pytest.raises(ValidationError):
        bundle(files)
    with pytest.raises(ValidationError):
        bundle([artifact()], instructions="a" * MAX_OUTPUT_BYTES)


def test_invalid_unicode_rejected() -> None:
    with pytest.raises(ValidationError):
        artifact("data.txt", "\ud800")


def test_output_schema_is_described_and_required() -> None:
    for model in (CodeFile, CodingOutput):
        schema = model.model_json_schema()
        assert len(schema["properties"]) == len(schema["required"])
        assert all(field["description"] for field in schema["properties"].values())
        assert schema["additionalProperties"] is False
