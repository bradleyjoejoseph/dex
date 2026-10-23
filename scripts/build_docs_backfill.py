#!/usr/bin/env python3
"""Build the docs section of a release that predates the documentation site.

Usage, from a checkout of the old release with the first site's build overlaid
(`docs/`, `scripts/build_docs_site.py` and `scripts/check_no_em_dashes.py` from
`BACKFILL_SCRIPT_TAG` in `.github/workflows/docs-site.yml`):

    uv run --project packages/dex-core python \
        <this repo>/scripts/build_docs_backfill.py --out DIR --release vX.Y.Z

The site's version switcher shows older minor releases as they were published,
each rebuilt from its own tag by its own `build_docs_site.py`. A release from
before the site has no such script, but its `references/` and its CLI are the
same kind of source the first site was built from, so it is built with that
first site's script, over its own references, changelog and CLI.

Two things the old release cannot satisfy are relaxed, and only these two:

  - menu pages it has no source for are left out of its menu, since a command
    group added later has no page in a release that did not have it;
  - the style rules on built text are not applied: they were written after the
    release, and an archive shows its text as it was.

Every other check of the first site's script still runs, so a link that does not
resolve still fails the build.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_site_script(root: Path):
    path = root / "scripts" / "build_docs_site.py"
    if not path.is_file():
        raise SystemExit(f"{path} is missing: overlay the first site's script first")
    spec = importlib.util.spec_from_file_location("build_docs_site", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass resolves its own module by name.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def prune(node: Any, keep: set[str]) -> Any:
    """A navigation tree without the pages outside `keep`, or None if empty."""

    if isinstance(node, str):
        return node if node in keep else None
    if isinstance(node, list):
        kept = [item for item in (prune(item, keep) for item in node) if item]
        return kept or None
    if isinstance(node, dict):
        out = dict(node)
        for key in ("tabs", "groups", "pages", "anchors", "dropdowns"):
            if key in node:
                kept = prune(node[key], keep)
                if kept is None:
                    return None
                out[key] = kept
        return out
    return node


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="build_docs_backfill.py",
        description="Build the docs section of a release from before the site.",
    )
    parser.add_argument("--out", type=Path, required=True, help="output directory")
    parser.add_argument("--release", required=True, help="the tag being built")
    args = parser.parse_args(argv)

    root = Path.cwd()
    site = _load_site_script(root)
    builder = site.SiteBuilder(
        repo_root=root,
        ref=args.release,
        release=args.release,
        em_dash_check=lambda text: [],
    )
    builder.collect()
    navigation = builder.config.get("navigation", {})
    dropped = sorted(set(site._navigation_pages(navigation)) - set(builder.pages))
    builder.config["navigation"] = prune(navigation, set(builder.pages)) or {}
    builder.validate()
    builder.check_output()
    problems = sorted(set(builder.problems))
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
        print(f"\n{len(problems)} problem(s); nothing written.", file=sys.stderr)
        return 1
    written = builder.write(args.out)
    print(
        f"Wrote {len(written)} files ({len(builder.pages)} pages) for {args.release} "
        f"to {args.out}. Left out of its menu: {', '.join(dropped) or 'nothing'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
