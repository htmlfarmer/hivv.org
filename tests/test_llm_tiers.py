#!/usr/bin/env python3
"""
Unit tests for hivv-cli's llm tier logic.

Verifies that `_llm_pick_model(specs)` returns the right tier and primary
recommendation for representative hardware profiles.

Run:  python3 tests/test_llm_tiers.py
       (from inside the repo's public_html/ — wherever it is cloned)

Stdlib only — no pytest. Exit 0 = all pass, exit 1 = any failure.
"""

import os
import sys
import importlib.util

# Load the CLI as a module without triggering main().
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.normpath(os.path.join(THIS_DIR, "..", "hivv-cli.py"))

spec = importlib.util.spec_from_file_location("hivv_cli", CLI_PATH)
hivv_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hivv_cli)

_pick = hivv_cli._llm_pick_model
_TIERS = hivv_cli._LLM_TIERS


# ── Test fixtures: (label, specs, expected_tier, expected_primary) ───────
CASES = [
    # 8-core no-GPU is the user's primary target — must be gemma4:e2b.
    ("8c / no-GPU / 16 GB", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 8, "ram_gb": 16.0,
        "gpu": None, "apple_silicon": False,
    }, "no-gpu-8core", "gemma4:e2b"),

    ("16c / no-GPU / 32 GB", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 16, "ram_gb": 32.0,
        "gpu": None, "apple_silicon": False,
    }, "no-gpu-8core", "gemma4:e2b"),

    # 4-7 cores no-GPU: small qwen primary.
    ("4c / no-GPU / 8 GB", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 4, "ram_gb": 8.0,
        "gpu": None, "apple_silicon": False,
    }, "no-gpu-low", "qwen3:1.7b"),

    ("7c / no-GPU / 16 GB", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 7, "ram_gb": 16.0,
        "gpu": None, "apple_silicon": False,
    }, "no-gpu-low", "qwen3:1.7b"),

    # <4 cores no-GPU: qwen3:0.6b (the "qwen .7b" user mentioned).
    ("2c / no-GPU / 4 GB", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 2, "ram_gb": 4.0,
        "gpu": None, "apple_silicon": False,
    }, "no-gpu-tiny", "qwen3:0.6b"),

    # NVIDIA tier (this dev box).
    ("16c / RTX 3050 / 6 GB VRAM", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 16, "ram_gb": 46.8,
        "gpu": {"vendor": "nvidia", "name": "RTX 3050", "vram_gb": 6.0},
        "apple_silicon": False,
    }, "nvidia-8gb", "qwen3:8b-q4"),

    # Big NVIDIA box.
    ("32c / RTX 4090 / 24 GB VRAM", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 32, "ram_gb": 64.0,
        "gpu": {"vendor": "nvidia", "name": "RTX 4090", "vram_gb": 24.0},
        "apple_silicon": False,
    }, "nvidia-big", "qwen3:14b"),

    # Apple Silicon.
    ("10c / M2 / 16 GB", {
        "system": "Darwin", "machine": "arm64", "release": "23.0",
        "python": "3.11", "cpu_count": 10, "ram_gb": 16.0,
        "gpu": None, "apple_silicon": True,
    }, "apple-silicon", "qwen3:4b"),

    # Edge: missing RAM (should not crash; falls back to smallest model — safe default).
    ("8c / no-GPU / RAM unknown", {
        "system": "Linux", "machine": "x86_64", "release": "6.1",
        "python": "3.11", "cpu_count": 8, "ram_gb": None,
        "gpu": None, "apple_silicon": False,
    }, "fallback", "qwen3:0.6b"),
]


def run():
    passed = failed = 0
    print(f"hivv-cli.py llm tier tests — {len(CASES)} cases\n")
    for label, specs, want_tier, want_primary in CASES:
        tier, models, reason = _pick(specs)
        primary = models[0][0] if models else None
        ok_tier    = tier == want_tier
        ok_primary = want_primary is None or primary == want_primary
        ok = ok_tier and ok_primary
        status = "✓" if ok else "✗"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {status} {label}")
        if not ok_tier:
            print(f"      tier:   got {tier!r}, want {want_tier!r}")
        if not ok_primary:
            print(f"      primary: got {primary!r}, want {want_primary!r}")
        print(f"      → {tier}  primary={primary}  ({reason})")
    print()
    print(f"  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
