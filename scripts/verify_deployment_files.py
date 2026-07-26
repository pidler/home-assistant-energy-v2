from __future__ import annotations

import filecmp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_APPS = ROOT / "apps"
DEPLOY_APPS = ROOT / "deploy" / "appdaemon" / "apps"


def relative_files(root: Path) -> set[Path]:
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def main() -> int:
    if not DEPLOY_APPS.exists():
        print(f"Missing deployment apps directory: {DEPLOY_APPS}", file=sys.stderr)
        return 1

    source_files = relative_files(SOURCE_APPS)
    deploy_files = relative_files(DEPLOY_APPS)

    missing = sorted(source_files - deploy_files)
    extra = sorted(deploy_files - source_files)
    changed = sorted(
        rel_path
        for rel_path in source_files & deploy_files
        if not filecmp.cmp(SOURCE_APPS / rel_path, DEPLOY_APPS / rel_path, shallow=False)
    )

    if missing or extra or changed:
        for label, paths in (("missing", missing), ("extra", extra), ("changed", changed)):
            for path in paths:
                print(f"{label}: {path}", file=sys.stderr)
        return 1

    print(f"Deployment files match source apps: {DEPLOY_APPS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
