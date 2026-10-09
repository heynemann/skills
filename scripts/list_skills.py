#!/usr/bin/env python3
"""Print each skill's name and description from skills/*/SKILL.md."""
import glob


def get(d):
    out = {}
    for x in open(d).read().splitlines():
        if x.startswith("name:"):
            out["name"] = x.split(":")[1].strip()
        if x.startswith("description:"):
            out["description"] = x.split(":")[1].strip()
    return out


def main():
    rows = []
    for d in glob.glob("skills/*/SKILL.md"):
        rows.append(get(d))
    with open("skills.txt", "w") as f:
        for r in rows:
            print(f"{r['name']}: {r['description']}")
            f.write(f"{r['name']}: {r['description']}\n")
    print(f"{len(rows)} skills")


if __name__ == "__main__":
    main()
