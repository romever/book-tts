class BookTtsError(Exception):
    """Expected domain or provider failure that can be shown to the user."""


class ParseError(BookTtsError):
    pass


class PrecheckError(BookTtsError):
    def __init__(
        self,
        message: str,
        *,
        segment_index: int | None = None,
        surface: str | None = None,
        source_text: str | None = None,
        response_path: str | None = None,
        content_path: str | None = None,
    ):
        super().__init__(message)
        self.segment_index = segment_index
        self.surface = surface
        self.source_text = source_text
        self.response_path = response_path
        self.content_path = content_path


class ProviderError(BookTtsError):
    def __init__(
        self,
        message: str,
        retryable: bool = False,
        response_path: str | None = None,
        content_rejected: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.response_path = response_path
        # 服务商明确以内容审核为由拒绝，用于触发本地模型接管
        self.content_rejected = content_rejected


class PackagingError(BookTtsError):
    pass
