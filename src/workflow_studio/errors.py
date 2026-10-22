"""Safe application error categories and Chinese presentation."""


class StorageError(Exception):
    """Persistence failure, independent of the Workflow business outcome."""


class UnsafeStoragePathError(StorageError, ValueError):
    pass


class StorageWriteError(StorageError):
    pass


class StorageReadError(StorageError):
    pass


class RunNotFoundError(StorageError):
    pass


class CorruptRunError(StorageError):
    pass


class RunConflictError(StorageError):
    pass


class InvalidResultError(StorageError, ValueError):
    pass


class UnsafeCodePathError(ValueError):
    pass


class ExportLimitError(ValueError):
    pass


MESSAGES = {
    "CheckpointConflictError": "任务正在其他执行中、检查点已变化或当前状态不允许此操作；请重新打开已保存任务后继续。",
    "CheckpointError": "无法读取或继续检查点；请确认任务标识、版本及本地存储状态。",
    "LLMConfigurationError": "API 配置无效或 Key 缺失，请检查 .env。",
    "LLMAuthenticationError": "API 认证或权限失败，请检查 Key 和账户权限；不会自动重试认证错误。",
    "LLMRateLimitError": "API 请求受到限流，有限重试已结束，请稍后手动重试。",
    "LLMTimeoutError": "模型请求超时，已停止当前节点。代码生成可能耗时较长，可在 .env 中设置 DEEPSEEK_TIMEOUT=300 后重新启动任务；若仍超时，请检查网络、代理和服务状态，或缩小需求范围。",
    "LLMNetworkError": "无法连接模型服务，请检查网络和代理设置。",
    "LLMStructuredOutputError": "模型响应未通过结构化输出校验。",
    "LLMTruncatedOutputError": "模型输出被截断，未将不完整代码标为成功，请缩小需求后重试。",
    "RequirementsOutputError": "需求分析输出校验失败，有限格式修复已用尽。",
    "DesignOutputError": "系统设计未通过结构或需求一致性校验，有限格式修复已用尽。",
    "ReviewOutputError": "审查输出未通过结构校验，有限格式修复已用尽；这不属于普通审查不通过。",
    "CodingOutputError": "代码文件未通过结构、安全或大小校验，有限格式修复已用尽。",
    "WorkflowStateError": "工作流 State 或上下游数据校验失败，已停止执行。",
    "UnsafeCodePathError": "代码路径不安全，已拒绝导出；请使用安全相对路径。",
    "UnsafeStoragePathError": "输出或历史路径不安全，请使用独立的本地输出目录和合法 run_id。",
    "StorageWriteError": "结果保存失败，请检查输出目录写入权限、磁盘空间和文件占用。当前会话结果仍保留，可仅重试保存。",
    "StorageReadError": "无法读取历史目录或文件，请检查读取权限和文件占用。",
    "RunNotFoundError": "找不到该运行记录，请确认 run_id 或刷新历史列表。",
    "CorruptRunError": "历史记录损坏、不完整或未通过 Schema/完整性校验，无法打开；其他运行记录仍可使用。",
    "RunConflictError": "该 run_id 已存在不同结果，已拒绝覆盖。请保留原记录并使用新的运行。",
    "InvalidResultError": "结果未通过 Schema 校验，未写入磁盘。当前会话结果仍保留。",
    "ExportLimitError": "导出内容超出文件数量或大小限制，已停止导出。",
    "ValidationError": "数据未通过 Pydantic 校验，已停止当前操作。",
    "RuntimeError": "工作流执行失败：节点执行发生内部异常（RuntimeError），已停止下游执行。",
    "LLMCallError": "模型服务调用失败，请检查模型配置与服务状态。",
}


def friendly_error(error: str | Exception | None) -> str:
    # Never echo an unrecognised string or exception body supplied by a provider.
    text = type(error).__name__ if isinstance(error, Exception) else error or ""
    for name, message in MESSAGES.items():
        if name in text:
            return message
    return "操作失败，已停止当前操作。请查看执行日志中的安全错误类别后重试。"
