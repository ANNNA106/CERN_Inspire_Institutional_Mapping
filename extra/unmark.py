"""
unmark.py
==========
Removes curation decisions for one or more institutions — pulls them back
OUT of v_ready_for_pushback if they haven't been pushed live yet.

Wraps db_manager.unmark_curation() for each control_number given.

Usage:
    python unmark.py 5003 5004 5005
        # removes curation rows for these three control numbers

    python unmark.py 5003 --force
        # also removes it even if already successfully pushed live
        # (does NOT touch the live inspirebeta.net record — only the
        # local curation/audit row)
"""
import argparse

from db_manager import unmark_curation


def main():
    l = [911423, 903883, 911155, 1866330, 2960665, 3071934, 3066279, 911313]

    for cn in l:
        unmark_curation(cn)


if __name__ == "__main__":
    main()