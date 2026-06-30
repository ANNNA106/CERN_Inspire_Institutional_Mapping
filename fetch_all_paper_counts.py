
# fetch_all_paper_counts.py
#
# Run this once to populate paper_count for all institutions.

from db_manager import get_conn, fetch_paper_counts


def main():
    conn = get_conn()
    cur = conn.cursor()

    # Fetch all institution control numbers
    cur.execute("""
        SELECT control_number
        FROM institutions
        ORDER BY control_number
    """)

    control_numbers = [row[0] for row in cur.fetchall()]

    conn.close()

    print(f"Fetching paper counts for {len(control_numbers)} institutions...")
    print(
        "Estimated time: ~{:.0f} minutes".format(
            len(control_numbers) * 0.2 / 60
        )
    )

    fetch_paper_counts(control_numbers)


if __name__ == "__main__":
    main()

