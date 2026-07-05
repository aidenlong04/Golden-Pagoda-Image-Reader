"""Tests for the startup cache pre-warm + OCR fast-failover settings."""
from __future__ import annotations

import cards
import ocr_engine


def test_warm_font_cache_populates_lru():
    cards._load_font_cached.cache_clear()
    cards.warm_font_cache()
    info = cards._load_font_cached.cache_info()
    assert info.currsize > 0
    # Everything warmed must fit the LRU or the warm evicts itself.
    assert info.currsize <= info.maxsize


def test_warm_font_cache_idempotent():
    cards.warm_font_cache()
    first = cards._load_font_cached.cache_info().currsize
    cards.warm_font_cache()
    assert cards._load_font_cached.cache_info().currsize == first


def test_ocr_connect_timeout_is_short():
    # A refused/blackholed OCR backend must fail over in seconds, not
    # after the full read timeout.
    assert 0 < ocr_engine.OCR_CONNECT_TIMEOUT <= 10
    assert ocr_engine.OCR_CONNECT_TIMEOUT < ocr_engine.OLLAMA_TIMEOUT
