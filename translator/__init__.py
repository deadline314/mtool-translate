"""MTool 翻譯器核心（不依賴介面）。"""
__version__ = "1.0.0"  # 唯一的版本號來源；發行時由 build.py release --version 更新

from .engines import (CATEGORIES, PROVIDER_BY_ID, PROVIDERS, ChatEngine, ClaudeEngine, Engine, GoogleFreeEngine,
                      LineEngine, MTEngine, MyMemoryEngine, OllamaEngine, OpenAICompatEngine, Provider, SakuraEngine,
                      make_engine)
from .errors import Cancelled, FatalError, RateLimitError, RequestError, TranslatorError, TransientError
from .formats import FORMATS, MTOOL_JSON, TranslationFormat, read_json, write_json_atomic
from .pipeline import PHASE_TERMS, PHASE_TRANSLATE, Job, JobConfig, Progress
from .text import LANGS, normalize_glossary

__all__ = [
    "CATEGORIES", "PROVIDERS", "PROVIDER_BY_ID", "Provider", "make_engine",
    "Engine", "ChatEngine", "LineEngine", "MTEngine",
    "OpenAICompatEngine", "OllamaEngine", "SakuraEngine", "ClaudeEngine", "MyMemoryEngine", "GoogleFreeEngine",
    "TranslatorError", "FatalError", "RateLimitError", "RequestError", "TransientError", "Cancelled",
    "FORMATS", "MTOOL_JSON", "TranslationFormat", "read_json", "write_json_atomic",
    "Job", "JobConfig", "Progress", "PHASE_TRANSLATE", "PHASE_TERMS",
    "LANGS", "normalize_glossary", "__version__",
]
