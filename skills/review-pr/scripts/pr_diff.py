#!/usr/bin/env python3
"""Line-number a PR diff and validate review comments against it.

Usage:
  gh pr diff <n> | pr_diff.py annotate              # print the diff with line labels
  gh pr diff <n> | pr_diff.py check review.json     # validate review.json comments

Labels: [OLD:n] deleted line (side LEFT), [NEW:m] added line (side RIGHT),
[OLD:n,NEW:m] unchanged context (side RIGHT, line m). GitHub only accepts
inline comments on labelled lines, and a range must stay inside one hunk.
"""
import json
import re
import sys

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse(lines):
    """Yield (path, hunk_index, label, side_lines, text) for every diff line.

    side_lines maps "LEFT"/"RIGHT" to the line number on that side, if any.
    Header lines carry path/hunk None so annotate can echo them.
    """
    path, hunk, old, new = None, -1, 0, 0
    old_path = None
    for raw in lines:
        line = raw.rstrip("\n")
        if line.startswith("diff --git "):
            path, old_path, hunk = None, None, -1
            yield None, None, "", {}, line
            continue
        if line.startswith("--- ") and hunk < 0:
            old_path = line[4:].removeprefix("a/")
            yield None, None, "", {}, line
            continue
        if line.startswith("+++ ") and hunk < 0:
            target = line[4:]
            path = old_path if target == "/dev/null" else target.removeprefix("b/")
            yield None, None, "", {}, line
            continue
        m = HUNK.match(line)
        if m and path:
            hunk += 1
            old, new = int(m.group(1)), int(m.group(2))
            yield None, None, "", {}, line
            continue
        if hunk < 0 or not path or line.startswith("\\"):
            yield None, None, "", {}, line
            continue
        if line.startswith("+"):
            yield path, hunk, f"[NEW:{new}]", {"RIGHT": new}, line
            new += 1
        elif line.startswith("-"):
            yield path, hunk, f"[OLD:{old}]", {"LEFT": old}, line
            old += 1
        else:
            yield path, hunk, f"[OLD:{old},NEW:{new}]", {"LEFT": old, "RIGHT": new}, line
            old += 1
            new += 1


def annotate(lines):
    for path, _, label, _, text in parse(lines):
        print(f"{label} {text}" if path else text)


def index(lines):
    """{(path, side, line): hunk}"""
    out = {}
    for path, hunk, _, sides, _ in parse(lines):
        if path:
            for side, n in sides.items():
                out[(path, side, n)] = hunk
    return out


def check(lines, review_path):
    with open(review_path) as f:
        review = json.load(f)
    idx = index(lines)
    errors = []
    for i, c in enumerate(review.get("comments", [])):
        where = f"comments[{i}] {c.get('path')}:{c.get('line')}"
        if not c.get("body"):
            errors.append(f"{where}: empty body")
        side = c.get("side", "RIGHT")
        end = idx.get((c.get("path"), side, c.get("line")))
        if end is None:
            errors.append(f"{where}: line {c.get('line')} ({side}) is not in the diff")
            continue
        if "start_line" in c:
            start_side = c.get("start_side", side)
            start = idx.get((c["path"], start_side, c["start_line"]))
            if start is None:
                errors.append(f"{where}: start_line {c['start_line']} ({start_side}) is not in the diff")
            elif start != end:
                errors.append(f"{where}: range crosses hunks")
            elif start_side == side and c["start_line"] >= c["line"]:
                errors.append(f"{where}: start_line must be below line")
    for e in errors:
        print(e)
    n = len(review.get("comments", []))
    print(f"{len(errors)} error(s)" if errors else f"{n}/{n} comments valid")
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "annotate":
        annotate(sys.stdin)
    elif len(sys.argv) == 3 and sys.argv[1] == "check":
        sys.exit(check(sys.stdin, sys.argv[2]))
    else:
        sys.exit(__doc__)
