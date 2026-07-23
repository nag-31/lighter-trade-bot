"""Merge an incoming dotenv file into a target without dropping target-only keys.

Values are never printed. The target is replaced atomically and retains its
existing permission bits.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path


KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def _values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = KEY_LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2)
    return values


def merge_env(target: Path, incoming: Path) -> tuple[int, int]:
    incoming_values = _values(incoming)
    target_lines = target.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    replaced = 0
    output: list[str] = []

    for line in target_lines:
        match = KEY_LINE.match(line)
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if key in incoming_values:
            output.append(f"{key}={incoming_values[key]}")
            seen.add(key)
            replaced += 1
        else:
            output.append(line)

    added_keys = [key for key in incoming_values if key not in seen]
    if added_keys:
        if output and output[-1]:
            output.append("")
        output.append("# Added from the latest local .env deployment")
        output.extend(f"{key}={incoming_values[key]}" for key in added_keys)

    mode = target.stat().st_mode & 0o777
    temp = target.with_name(f".{target.name}.merge-{os.getpid()}")
    try:
        temp.write_text("\n".join(output) + "\n", encoding="utf-8")
        os.chmod(temp, mode)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return replaced, len(added_keys)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--incoming", required=True, type=Path)
    args = parser.parse_args()
    replaced, added = merge_env(args.target, args.incoming)
    print(f"merged environment keys: replaced={replaced} added={added}")


if __name__ == "__main__":
    main()
