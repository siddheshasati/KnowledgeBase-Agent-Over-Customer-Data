from pathlib import Path


FEATURE_ROW = (
    "| Autonomous low-battery return-to-dock policy | fleet | new | Meridian AgriTech, Northfall Security Group | 2 | $88,000 |"
)


def main() -> None:
    path = Path("se-dataset") / "feature_requests.md"
    text = path.read_text(encoding="utf-8")
    if FEATURE_ROW in text:
        updated = text.replace(
            FEATURE_ROW,
            "| Autonomous low-battery return-to-dock policy | fleet | in_progress | Meridian AgriTech, Northfall Security Group, Blue Harbor Logistics | 3 | $112,000 |",
        )
        action = "updated"
    else:
        updated = text.rstrip() + "\n" + FEATURE_ROW + "\n"
        action = "added"
    path.write_text(updated, encoding="utf-8")
    print(f"Demo record {action}. Run POST /api/ingest/sync-now or python scripts/ingest_once.py next.")


if __name__ == "__main__":
    main()
