"""解析后端注册表；接口目录与任务调度共用这一份定义。"""
from .base import DocumentParser, ParsedDocument
from .pymupdf import PyMuPDFParser

_registry: dict[str, DocumentParser] = {}


def register(parser: DocumentParser) -> None:
    if parser.id in _registry:
        raise ValueError(f"解析后端已注册：{parser.id}")
    _registry[parser.id] = parser


def get_parser(parser_id: str) -> DocumentParser:
    try:
        return _registry[parser_id]
    except KeyError:
        raise ValueError(f"未知解析后端：{parser_id}") from None


def is_available(parser: DocumentParser) -> bool:
    """后端可声明 available()；未启用的后端不出现在方案目录中。"""
    check = getattr(parser, "available", None)
    return bool(check()) if callable(check) else True


def ensure_available(parser_id: str) -> DocumentParser:
    parser = get_parser(parser_id)
    if not is_available(parser):
        raise ValueError(f"解析方案「{parser.name}」未启用，请联系管理员配置服务地址")
    return parser


def catalog() -> list[dict]:
    return [{"id": parser.id, "name": parser.name, "local_ocr_options": parser.local_ocr_options}
            for parser in _registry.values() if is_available(parser)]


register(PyMuPDFParser())
