"""Clean-checkout start instructions work: env path and frontend bundle.

The quickstart told readers to copy .env.example from inside
apps/classification-evals, but that file only exists at the repository
root, so the documented copy failed. start.sh also never built the
workbench frontend bundle, so / served a missing-bundle 404 on a clean
checkout. The documented copy paths now resolve, and start.sh builds the
bundle (or fails with a short error) before serving.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
START_SH = REPO_ROOT / "apps" / "classification-evals" / "start.sh"
ENSURE_FRONTEND_SH = REPO_ROOT / "apps" / "classification-evals" / "scripts" / "ensure_frontend.sh"

FAKE_NPM = """#!/usr/bin/env bash
if [ "$1" = "run" ] && [ "$2" = "build" ]; then
  mkdir -p dist
  printf '<html></html>\\n' > dist/index.html
fi
exit 0
"""


class DocumentedEnvPathsTest(unittest.TestCase):
    """Every documented `cp <src> <dst>` inside a `cd <dir>` block resolves."""

    DOCS = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "apps" / "classification-evals" / "README.md",
    )
    CD_RE = re.compile(r"^cd\s+(\S+)\s*$")
    CP_RE = re.compile(r"^cp\s+(\S+)\s+(\S+)\s*$")
    FENCE_RE = re.compile(r"^```")

    def test_documented_cp_sources_exist(self):
        failures = []
        for doc in self.DOCS:
            in_fence = False
            cwd = "."
            for line in doc.read_text().splitlines():
                if self.FENCE_RE.match(line):
                    in_fence = not in_fence
                    continue
                if not in_fence:
                    continue
                cd = self.CD_RE.match(line)
                if cd:
                    cwd = cd.group(1)
                    continue
                cp = self.CP_RE.match(line)
                if cp:
                    source = (REPO_ROOT / cwd / cp.group(1)).resolve()
                    if not source.exists():
                        failures.append(f"{doc.name}: {line.strip()} -> {source} missing")
        self.assertEqual(failures, [])


class EnsureFrontendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.product_root = root / "product"
        frontend = self.product_root / "frontend"
        frontend.mkdir(parents=True)
        (frontend / "package.json").write_text('{"name": "fake-frontend"}\n')

    def _bin_dir(self, with_npm: bool) -> Path:
        bin_dir = Path(self.tmp.name) / ("bin-with-npm" if with_npm else "bin-no-npm")
        bin_dir.mkdir()
        for tool in ("bash", "dirname", "pwd", "mkdir", "printf", "cat"):
            found = shutil.which(tool)
            if found:
                os.symlink(found, bin_dir / tool)
        if with_npm:
            npm = bin_dir / "npm"
            npm.write_text(FAKE_NPM)
            npm.chmod(0o755)
        return bin_dir

    def _run(self, script: Path, bin_dir: Path, extra_env=None):
        env = {
            "PATH": str(bin_dir),
            "HOME": os.environ.get("HOME", ""),
            "PRODUCT_APP_ROOT": str(self.product_root),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [BASH, str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_builds_missing_bundle_with_npm(self):
        result = self._run(ENSURE_FRONTEND_SH, self._bin_dir(with_npm=True))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.product_root / "frontend" / "dist" / "index.html").exists())

    def test_existing_bundle_skips_build(self):
        dist = self.product_root / "frontend" / "dist"
        dist.mkdir()
        (dist / "index.html").write_text("<html>existing</html>")
        npm_bin = self._bin_dir(with_npm=True)
        npm_bin.joinpath("npm").write_text("#!/usr/bin/env bash\nexit 99\n")
        npm_bin.joinpath("npm").chmod(0o755)
        result = self._run(ENSURE_FRONTEND_SH, npm_bin)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("existing", (dist / "index.html").read_text())

    def test_missing_npm_gives_short_error_and_build_hint(self):
        result = self._run(ENSURE_FRONTEND_SH, self._bin_dir(with_npm=False))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm not found", result.stderr)
        self.assertIn("npm ci", result.stderr)
        self.assertIn("npm run build", result.stderr)

    def test_broken_build_fails_rather_than_serving_404(self):
        npm_bin = self._bin_dir(with_npm=True)
        # A build that does not produce the bundle must fail the start.
        npm_bin.joinpath("npm").write_text("#!/usr/bin/env bash\nexit 0\n")
        npm_bin.joinpath("npm").chmod(0o755)
        result = self._run(ENSURE_FRONTEND_SH, npm_bin)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still missing", result.stderr)

    def test_start_sh_fails_fast_without_bundle_and_npm(self):
        state_dir = Path(self.tmp.name) / "var"
        result = self._run(
            START_SH,
            self._bin_dir(with_npm=False),
            extra_env={"CLASSIFY_EVAL_STATE_DIR": str(state_dir)},
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("npm not found", result.stderr)
        # start.sh must stop before creating its virtualenv or serving.
        self.assertFalse((REPO_ROOT / "apps" / "classification-evals" / ".venv").exists())

    def test_shell_scripts_have_valid_syntax(self):
        for script in (START_SH, ENSURE_FRONTEND_SH):
            result = subprocess.run(
                [BASH, "-n", str(script)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
