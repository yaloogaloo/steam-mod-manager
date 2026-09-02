"""Map archive extraction failures to stable deploy error codes."""

from __future__ import annotations


def archive_error_code(message: str) -> str:
    text = (message or "").strip().lower()
    if "超时" in message or "timeout" in text:
        return "ARCHIVE_TIMEOUT"
    if "不存在" in message or "not found" in text:
        return "ARCHIVE_NOT_FOUND"
    if "架构不兼容" in message or "executable invalid" in text or "winerror 216" in text:
        return "ARCHIVE_EXECUTABLE_INVALID"
    if "解压组件" in message or "unrar" in text or "extractor" in text:
        return "EXTRACTOR_NOT_AVAILABLE"
    if "分卷" in message or "first volume" in text:
        return "ARCHIVE_MULTIVOLUME"
    if "不安全" in message or "zip-slip" in text or "traversal" in text:
        return "ARCHIVE_SECURITY_VIOLATION"
    if "加密" in message or "password" in text:
        return "ARCHIVE_ENCRYPTED"
    if "不支持" in message or "unsupported" in text:
        return "ARCHIVE_UNSUPPORTED"
    if "损坏" in message or "corrupt" in text:
        return "ARCHIVE_CORRUPT"
    return "ARCHIVE_EXTRACT_FAILED"