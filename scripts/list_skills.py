#!/usr/bin/env python3
"""Print each skill's name and description from skills/*/SKILL.md."""
import glob


def read_skill_fields(path):
    fields = {}
    lines = open(path).read().splitlines()
    if lines[:1] != ["---"]:
        return fields
    for line in lines[1:]:
        if line == "---":
            break
        if line.startswith("name:"):
            fields["name"] = line.split(":", 1)[1].strip()
        if line.startswith("description:"):
            fields["description"] = line.split(":", 1)[1].strip()
    return fields


def main():
    rows = []
    for path in glob.glob("skills/*/SKILL.md"):
        fields = read_skill_fields(path)
        if "name" in fields and "description" in fields:
            rows.append(fields)
    for r in sorted(rows, key=lambda r: r["name"]):
        print(f"{r['name']}: {r['description']}")


if __name__ == "__main__":
    main()
