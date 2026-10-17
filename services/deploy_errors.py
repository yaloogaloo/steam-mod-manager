"""Deploy-time errors distinct from copy/IO failures."""

from __future__ import annotations


class DeploySourceError(Exception):
    """
    Deploy source is missing, empty, archives-only, or not yet extracted.

    Raised before ``strategy.deploy`` so unknown managed folders are never
    treated as a silent success path.
    """

    def __init__(
        self,
        message: str = "",
        *,
        code: str = "",
        missing_files: list[str] | None = None,
        replacement_candidates: list[dict[str, str]] | None = None,
        source_changed: list[str] | None = None,
    ) -> None:
        self.code = str(code or "").strip()
        self.missing_files: list[str] = list(missing_files or [])
        self.replacement_candidates: list[dict[str, str]] = list(
            replacement_candidates or []
        )
        self.source_changed: list[str] = list(source_changed or [])
        text = str(message or "").strip()
        if not text:
            if self.code == "replacement_required":
                text = "Mod 源文件版本不一致，需要刷新或手动选择版本"
            elif self.code == "missing_files":
                text = "Mod 源文件缺失"
            elif self.code == "no_deployable_source":
                text = "Mod 没有可部署的内容"
            else:
                text = "部署源无效"
        super().__init__(text)


class DeployValidationError(Exception):
    """
    Deploy copy appeared to succeed, but post-deploy target verification failed.

    Distinct from copy/permission failures raised during ``strategy.deploy``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        missing_targets: list[str] | None = None,
    ) -> None:
        self.missing_targets: list[str] = list(missing_targets or [])
        text = str(message or "").strip()
        if not text and self.missing_targets:
            text = (
                f"部署结果校验失败：缺少 {len(self.missing_targets)} 个目标文件"
            )
        elif not text:
            text = "部署结果校验失败"
        super().__init__(text)
