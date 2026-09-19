"""Fail if `deploy/trc/SOUL.md` has drifted from the backend's reviewed prompt (H2).

The TRC system prompt is reviewed in trc-backend
(`integrations/openwebui/SYSTEM_PROMPT.md`, inside its ```text block) because it is a
security control and belongs under the same review as the redaction filter. Hermes needs
its own copy on the deploy volume, because the IDENTITY tier is the only place a prompt
lands ahead of Hermes' own persona rather than 15 K characters behind it.

Two copies of a security control drift. This is the check that says so — the same shape
as any other generated-file check: regenerate, compare, fail with the diff.

Usage (CI runs the first form):
    python scripts/check_soul_matches_backend.py --backend ../trc-backend
    python scripts/check_soul_matches_backend.py --backend ../trc-backend --write

`--backend` defaults to a sibling checkout, which is the fleet layout
(`trc-orchastrator/submodules/*`). CI passes it explicitly.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

_SOUL = Path(__file__).resolve().parent.parent / "deploy" / "trc" / "SOUL.md"
_BLOCK_RE = re.compile(r"^```text\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def backend_prompt(backend_root: Path) -> str:
    """The prompt text inside the backend doc's ```text block."""
    source = backend_root / "integrations" / "openwebui" / "SYSTEM_PROMPT.md"
    match = _BLOCK_RE.search(source.read_text(encoding="utf-8"))
    if not match:
        raise SystemExit(f"no ```text block found in {source}")
    return match.group(1)


def soul_body(text: str) -> str:
    """`SOUL.md` minus its leading HTML comment header.

    The header explains where the file comes from and is not part of the prompt, so it
    is excluded from the comparison rather than duplicated into the backend doc.
    """
    if text.startswith("<!--"):
        _, _, rest = text.partition("-->")
        return rest.lstrip("\n")
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="../trc-backend", type=Path)
    parser.add_argument(
        "--write",
        action="store_true",
        help="rewrite SOUL.md's body from the backend prompt instead of failing",
    )
    args = parser.parse_args()

    expected = backend_prompt(args.backend)
    current = _SOUL.read_text(encoding="utf-8")
    if soul_body(current) == expected:
        print("SOUL.md matches the backend prompt")
        return 0

    if args.write:
        header, _, _ = current.partition("-->")
        _SOUL.write_text(f"{header}-->\n\n{expected}", encoding="utf-8")
        print(f"rewrote {_SOUL} from the backend prompt")
        return 0

    diff = difflib.unified_diff(
        expected.splitlines(),
        soul_body(current).splitlines(),
        fromfile="trc-backend/integrations/openwebui/SYSTEM_PROMPT.md",
        tofile="deploy/trc/SOUL.md",
        lineterm="",
    )
    print("\n".join(diff))
    print(
        "\nSOUL.md has drifted from the backend's reviewed prompt. Edit the BACKEND "
        "file, then re-run this with --write.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
