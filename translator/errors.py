"""錯誤分類。翻譯流程依類別決定：重試、拆小批重送、跳過，或中止整個工作。"""


class TranslatorError(Exception):
    """所有翻譯器錯誤的基底，訊息直接給使用者看。"""


class FatalError(TranslatorError):
    """設定或權限錯誤（金鑰錯、模型不存在、位址錯…）。重試沒有意義，中止整個工作。"""


class RateLimitError(FatalError):
    """重試後仍被限流或額度用完。中止工作，稍後再續翻。"""


class RequestError(TranslatorError):
    """這次請求的內容有問題（太長、被拒絕、輸出被截斷）。換小一點的批次可能就會成功。"""


class TransientError(TranslatorError):
    """暫時性錯誤（網路、伺服器忙、限流）。等一下重試。"""

    def __init__(self, message, retry_after=None, rate_limited=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.rate_limited = rate_limited


class Cancelled(TranslatorError):
    """使用者按了停止。不算失敗，下次會續翻。"""
