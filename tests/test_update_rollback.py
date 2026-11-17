#!/usr/bin/env python3
"""
Unit tests for hivv-cli's atomic-update-with-rollback helper.

Verifies that `_apply_update(data, expected_sha256=None, source=...)` does the
right thing in three scenarios:

  A. Valid sha + passing healthcheck → atomic swap succeeds.
  B. Wrong sha → refused before touching disk (no .new, no .bak change).
  C. Passing sha + failing healthcheck → .bak restored, .bad retained.

Plus a smoke test for the `--healthcheck` flag and `_parse_ver`.

Run:  python3 tests/test_update_rollback.py
       (from inside hivv.org/public_html/)
       or  python3 /var/www/vhosts/hivv.org/public_html/tests/test_update_rollback.py

Stdlib only — no pytest. Exit 0 = all pass, exit 1 = any failure.
"""

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── Load the CLI as a module without triggering main() ──────────────────
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.normpath(os.path.join(THIS_DIR, "..", "hivv-cli.py"))

spec = importlib.util.spec_from_file_location("hivv_cli", CLI_PATH)
hivv_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hivv_cli)

_apply_update = hivv_cli._apply_update
_parse_ver     = hivv_cli._parse_ver


# ── Fixture: a fake CLI that respects --healthcheck via env var ─────────
# We craft a tiny "binary" whose behavior is controlled by an env var
# FAKE_HEALTHCHECK_RC: 0 = pass, anything else = fail. This way one script
# covers both Case A (passing) and Case C (failing).
# sys.argv[0] is the script path itself; --healthcheck is sys.argv[1].
FAKE_CLI_SRC = '''#!/usr/bin/env python3
"""Fake CLI for _apply_update tests. Behavior controlled by env var."""
import os, sys
rc = int(os.environ.get("FAKE_HEALTHCHECK_RC", "0"))
if len(sys.argv) > 1 and sys.argv[1] == "--healthcheck":
    sys.exit(rc)
sys.exit(0)
'''


def _isolated_install_dir(monkeypatch_target: Path):
    """Return a context-manager-style helper: patch hivv_cli.INSTALL_DIR to a
    tmpdir so `_apply_update` operates on the fake CLI instead of the real one.
    Use as: with _isolated_install_dir(tmpdir_path): ...  (decorator-style)."""
    class _Ctx:
        def __enter__(self_inner):
            self_inner._saved = hivv_cli.INSTALL_DIR
            hivv_cli.INSTALL_DIR = monkeypatch_target
            return self_inner
        def __exit__(self_inner, *a):
            hivv_cli.INSTALL_DIR = self_inner._saved
            return False
    return _Ctx()


def _make_fake_cli(rc: int) -> str:
    """Return the source bytes for a fake CLI that exits `rc` on --healthcheck."""
    return FAKE_CLI_SRC.replace('"FAKE_HEALTHCHECK_RC", "0"',
                                f'"FAKE_HEALTHCHECK_RC", "{rc}"').encode()


def _setup_workspace(tmpdir: Path) -> tuple:
    """Lay down an `INSTALL_DIR/<APP>-cli.py` for testing.
    Returns (target_path, original_bytes).
    """
    inst = tmpdir / "INSTALL_DIR"
    inst.mkdir()
    target = inst / "hivv-cli.py"
    original = b"#!/usr/bin/env python3\n# original (good) binary\nprint('original')\n"
    target.write_bytes(original)
    return target, original


def case_a_happy_path():
    """A. Valid sha + passing healthcheck → swap succeeds, .bak left."""
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        target, original = _setup_workspace(tmpdir)
        with _isolated_install_dir(target.parent):
            new_data = _make_fake_cli(rc=0)
            new_sha = hashlib.sha256(new_data).hexdigest()
            ok, msg, returned_target = _apply_update(new_data, expected_sha256=new_sha, source="test")
        if not ok:
            return False, f"expected ok=True, got ok=False, msg={msg!r}"
        if returned_target != target:
            return False, f"target mismatch: got {returned_target}, want {target}"
        if target.read_bytes() != new_data:
            return False, "target file was not replaced with new_data"
        bak = target.with_suffix(target.suffix + ".bak")
        if not bak.exists():
            return False, ".bak should have been created"
        if bak.read_bytes() != original:
            return False, ".bak should contain the original bytes"
        new = target.with_suffix(target.suffix + ".new")
        if new.exists():
            return False, ".new should have been promoted/removed"
        bad = target.with_suffix(target.suffix + ".bad")
        if bad.exists():
            return False, ".bad should NOT exist on happy path"
    return True, ""


def case_b_sha_mismatch():
    """B. Wrong sha → refused before touching disk."""
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        target, original = _setup_workspace(tmpdir)
        with _isolated_install_dir(target.parent):
            new_data = _make_fake_cli(rc=0)
            wrong_sha = "0" * 64  # definitely wrong
            ok, msg, returned_target = _apply_update(new_data, expected_sha256=wrong_sha, source="test")
        if ok:
            return False, f"expected ok=False, got ok=True, msg={msg!r}"
        if "sha256" not in msg.lower():
            return False, f"expected sha256 mention in msg, got {msg!r}"
        if target.read_bytes() != original:
            return False, "target file should NOT have been touched"
        bak = target.with_suffix(target.suffix + ".bak")
        if bak.exists():
            return False, ".bak should NOT be created when sha mismatches"
        new = target.with_suffix(target.suffix + ".new")
        if new.exists():
            return False, ".new should NOT exist when sha mismatches"
    return True, ""


def case_c_healthcheck_fail():
    """C. Passing sha + failing healthcheck → .bak restored, .bad retained."""
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        target, original = _setup_workspace(tmpdir)
        with _isolated_install_dir(target.parent):
            new_data = _make_fake_cli(rc=1)  # healthcheck exits 1
            new_sha = hashlib.sha256(new_data).hexdigest()
            ok, msg, returned_target = _apply_update(new_data, expected_sha256=new_sha, source="test")
        if ok:
            return False, f"expected ok=False, got ok=True, msg={msg!r}"
        if "healthcheck" not in msg.lower():
            return False, f"expected healthcheck mention in msg, got {msg!r}"
        # target should be restored to original
        if target.read_bytes() != original:
            return False, "target should have been restored from .bak to original"
        # .bad should be retained for inspection
        bad = target.with_suffix(target.suffix + ".bad")
        if not bad.exists():
            return False, ".bad should be retained for inspection"
        if bad.read_bytes() != new_data:
            return False, ".bad should contain the failed new_data"
        # .new should be gone
        new = target.with_suffix(target.suffix + ".new")
        if new.exists():
            return False, ".new should NOT exist after healthcheck failure"
    return True, ""


def case_d_parse_ver():
    """D. _parse_ver tuple comparison — basic invariants."""
    cases = [
        (("0.1.0",   "0.1.0"),   False),
        (("0.2.0",   "0.1.0"),   True),
        (("0.1.0",   "0.2.0"),   False),
        (("1.0",     "0.9.9"),   True),
        (("0.10.0",  "0.9.0"),   True),    # numeric, not lexical
        (("0.1",     "0.1.0"),   False),   # shorter tuple is smaller? — depends on impl
    ]
    for (a, b), want in cases:
        got = _parse_ver(a) > _parse_ver(b)
        if got != want:
            return False, f"_parse_ver({a!r}) > _parse_ver({b!r}) = {got}, want {want}"
    return True, ""


def case_e_healthcheck_flag():
    """E. `python3 hivv-cli.py --healthcheck` exits 0 with JSON body."""
    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        # Copy CLI to a sandbox location so we can run it
        sandbox = tmpdir / "cli"
        sandbox.mkdir()
        run_cli = sandbox / "hivv-cli.py"
        # Symlink to the real CLI; but we need an isolated state dir for node_id
        # So just copy the bytes.
        shutil.copy(CLI_PATH, run_cli)
        # Use a fake HOME so it doesn't pollute ~/.local/share/hivv
        env = os.environ.copy()
        env["HOME"] = str(tmpdir / "home")
        env["PATH"] = "/usr/bin:/bin"
        proc = subprocess.run(
            [sys.executable, str(run_cli), "--healthcheck"],
            capture_output=True, timeout=10, env=env,
        )
        if proc.returncode != 0:
            return False, f"expected exit 0, got {proc.returncode}. stderr={proc.stderr.decode()}"
        try:
            payload = json.loads(proc.stdout.decode().strip().splitlines()[-1])
        except Exception as e:
            return False, f"could not parse JSON from stdout: {e}. stdout={proc.stdout.decode()[:300]!r}"
        for key in ("ok", "version"):
            if key not in payload:
                return False, f"missing key {key!r} in payload: {payload}"
        if payload.get("ok") is not True:
            return False, f"payload.ok should be True: {payload}"
    return True, ""


CASES = [
    ("A. happy path (sha OK + healthcheck pass)", case_a_happy_path),
    ("B. sha256 mismatch",                       case_b_sha_mismatch),
    ("C. healthcheck fail → rollback",           case_c_healthcheck_fail),
    ("D. _parse_ver tuple compare",              case_d_parse_ver),
    ("E. --healthcheck flag emits JSON",         case_e_healthcheck_flag),
]


def run():
    passed = failed = 0
    print(f"hivv-cli.py update-rollback tests — {len(CASES)} cases\n")
    for label, fn in CASES:
        try:
            ok, why = fn()
        except Exception as e:
            ok, why = False, f"raised {type(e).__name__}: {e}"
        status = "✓" if ok else "✗"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {status} {label}")
        if not ok:
            print(f"      {why}")
    print()
    print(f"  {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
