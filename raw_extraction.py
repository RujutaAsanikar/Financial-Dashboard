"""Bridge from a raw PDF/image upload to parser JSON, via data_extraction.py's
Claude-based extraction. Everything downstream of /api/upload already
assumes parser JSON; this is what makes a raw statement file look like one
before it reaches that point. Never called for .json uploads -- those
already are parser JSON.
"""

import logging
import tempfile
from pathlib import Path
from types import SimpleNamespace

import data_extraction as DE

logger = logging.getLogger(__name__)

SUPPORTED_RAW_TYPES = DE.SUPPORTED_TYPES  # .pdf/.png/.jpg/.jpeg/.gif/.webp


def is_raw_statement(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in SUPPORTED_RAW_TYPES


def extract(filename: str, raw: bytes) -> dict:
    """Run the Anthropic extractor on one raw file's bytes; return parser
    JSON in the same shape data_extraction.py writes to disk.

    Raises ValueError with a message safe to show the user on any failure --
    an unsupported extension, a missing API key, or the model itself failing
    after its own retries. Never lets SystemExit escape (the CLI script uses
    it for its own top-level error reporting, which would otherwise crash
    the request instead of returning a readable error).
    """
    suffix = Path(filename or "").suffix.lower()
    if suffix not in SUPPORTED_RAW_TYPES:
        raise ValueError(
            f"{filename}: unsupported file type. Supported: "
            f"{', '.join(sorted(SUPPORTED_RAW_TYPES))}, or .json (parser output)."
        )

    api_key = DE.resolve_api_key(None)
    if not api_key:
        raise ValueError(
            "No ANTHROPIC_API_KEY is configured on the server, so raw statement "
            "files can't be read yet. Upload the parser's .json output instead."
        )

    import anthropic

    with tempfile.TemporaryDirectory() as tmp_dir:
        path = Path(tmp_dir) / (Path(filename).name or f"statement{suffix}")
        path.write_bytes(raw)

        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
        args = SimpleNamespace(
            model=DE.MODEL_NAME,
            max_tokens=16384,
            no_schema=False,
            no_fallback=False,
            retries=5,
        )
        try:
            data, warnings = DE.extract_statement(client, path, args)
        except SystemExit as exc:
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            logger.exception("Extraction failed for %s", filename)
            raise ValueError(f"Could not read {filename}: {exc}") from exc

    if warnings:
        logger.warning("Extraction warnings for %s: %s", filename, "; ".join(warnings))

    return data
