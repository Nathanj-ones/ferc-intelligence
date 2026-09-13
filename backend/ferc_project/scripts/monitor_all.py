from collections import Counter

from ferc_filter.monitor import (
    FERCMonitor,
    MONITORED_PROJECTS,
)


def print_project_summary(result: dict) -> None:
    """Print a compact summary for one monitored project."""

    counts = Counter(
        item["decision"]
        for item in result["results"]
    )

    print()
    print(
        f"{result['project']} "
        f"({result['docket']})"
    )

    print(
        f"  Window: "
        f"{result['start_date']} -> "
        f"{result['end_date']}"
    )

    print(
        f"  Raw: {result['raw_count']} | "
        f"New: {result['new_count']} | "
        f"Processed: {result['processed_count']}"
    )

    print(
        f"  ALERT: {counts['ALERT']} | "
        f"REVIEW: {counts['REVIEW']} | "
        f"SUPPRESS: {counts['SUPPRESS']}"
    )

    for item in result["results"]:

        print(
            f"    {item['accession']} | "
            f"{item['decision']} | "
            f"{item['event_type']}"
        )

        print(
            f"      "
            f"{item['document_class']} -> "
            f"{item['document_type']}"
        )

        print(
            f"      {item['description']}"
        )


def main() -> None:
    """Run the monitor across every configured project."""

    print("=" * 80)
    print("FERC LIVE MONITOR")
    print("=" * 80)

    monitor = FERCMonitor()

    total_raw = 0
    total_new = 0
    total_processed = 0

    total_decisions = Counter()

    for project in MONITORED_PROJECTS:

        print()
        print("-" * 80)
        print(
            f"Checking {project.name} "
            f"({project.docket})..."
        )

        try:
            result = monitor.run_project(
                project=project,
                initial_lookback_days=7,
                overlap_days=1,
            )

        except Exception as exc:

            print(
                f"ERROR: {project.name}: {exc}"
            )

            continue

        total_raw += result["raw_count"]
        total_new += result["new_count"]
        total_processed += result[
            "processed_count"
        ]

        for item in result["results"]:
            total_decisions[
                item["decision"]
            ] += 1

        print_project_summary(result)

    print()
    print("=" * 80)
    print("TOTAL")
    print("=" * 80)

    print(
        f"Raw records checked: {total_raw}"
    )

    print(
        f"New filings: {total_new}"
    )

    print(
        f"Processed: {total_processed}"
    )

    print(
        f"ALERT: {total_decisions['ALERT']} | "
        f"REVIEW: {total_decisions['REVIEW']} | "
        f"SUPPRESS: {total_decisions['SUPPRESS']}"
    )


if __name__ == "__main__":
    main()