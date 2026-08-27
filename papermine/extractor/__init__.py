"""要素抽取子包：代码静态分析 + 主流文档与 OCR 文本分析。"""

from .doc_extractor import ExtractionResult, extract_asset_text, read_asset_text

__all__ = ["ExtractionResult", "extract_asset_text", "read_asset_text"]
