#!/usr/bin/env python3
"""Print each skill's name and description from skills/*/SKILL.md."""
import glob


def get(d):
    out = {}
    lines = open(d).read().splitlines()
    if lines[:1] != ["---"]:
        return out
    for x in lines[1:]:
        if x == "---":
            break
        if x.startswith("name:"):
            out["name"] = x.split(":", 1)[1].strip()
        if x.startswith("description:"):
            out["description"] = x.split(":", 1)[1].strip()
    return out


def main():
    rows = []
    for d in glob.glob("skills/*/SKILL.md"):
        r = get(d)
        if "name" in r and "description" in r:
            rows.append(r)
    with open("skills.txt", "w") as f:
        for r in rows:
            print(f"{r['name']}: {r['description']}")
            f.write(f"{r['name']}: {r['description']}\n")
    print(f"{len(rows)} skills")


if __name__ == "__main__":
    main()
