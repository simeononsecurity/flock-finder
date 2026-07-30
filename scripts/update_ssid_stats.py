#!/usr/bin/env python3
"""
update_ssid_stats.py — Regenerate SSID statistics in README.md and docs/index.html.

Reads all data/by_oui/*.csv files, computes:
  - Top 10 most frequent SSIDs (unfiltered)
  - Flock* SSID pattern breakdown

Then rewrites the comment-marker-delimited blocks in:
  - README.md      (<!-- SSID_TOP10_START/END -->, <!-- SSID_PATTERNS_START/END -->)
  - docs/index.html (<!-- SSID_TOP10_ROWS_START/END -->, <!-- SSID_TOP10_STATS_START/END -->,
                     <!-- SSID_PATTERNS_SUMMARY_START/END -->, <!-- SSID_PATTERNS_ROWS_START/END -->)

Run manually:  python3 scripts/update_ssid_stats.py
Or via GitHub Actions after wigle_query.py completes.
"""

import csv
import html as html_lib
import re
import sys
from collections import Counter
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = REPO_ROOT / "data" / "by_oui"
README = REPO_ROOT / "README.md"
INDEX_HTML = REPO_ROOT / "docs" / "index.html"


# ── Data collection ───────────────────────────────────────────────────────────
def collect_ssid_data():
    all_ssids: Counter = Counter()
    flock_ssids: Counter = Counter()
    total_ssid_records = 0
    oui_file_count = 0

    csv_files = sorted(DATA_DIR.glob("*.csv"))
    if not csv_files:
        print(f"WARNING: No CSV files found in {DATA_DIR}", file=sys.stderr)
        return all_ssids, flock_ssids, total_ssid_records, oui_file_count

    for fpath in csv_files:
        oui_file_count += 1
        with open(fpath, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ssid = row.get("ssid", "").strip()
                if not ssid:
                    continue
                all_ssids[ssid] += 1
                total_ssid_records += 1
                if ssid.lower().startswith("flock"):
                    flock_ssids[ssid] += 1

    return all_ssids, flock_ssids, total_ssid_records, oui_file_count


# ── Flock* pattern grouping ───────────────────────────────────────────────────
def group_flock_patterns(flock_ssids: Counter) -> dict:
    bare: Counter = Counter()
    dash_6hex: Counter = Counter()
    upper_dash_6hex: Counter = Counter()
    dash_4hex: Counter = Counter()
    numeric: Counter = Counter()
    other: Counter = Counter()

    for ssid, count in flock_ssids.items():
        if re.fullmatch(r"[Ff][Ll][Oo][Cc][Kk]", ssid):
            bare[ssid] += count
        elif re.fullmatch(r"Flock-[0-9A-Fa-f]{6}", ssid):
            dash_6hex[ssid] += count
        elif re.fullmatch(r"FLOCK-[0-9A-Fa-f]{6}", ssid):
            upper_dash_6hex[ssid] += count
        elif re.fullmatch(r"[Ff][Ll][Oo][Cc][Kk]-[0-9A-Fa-f]{4}", ssid):
            dash_4hex[ssid] += count
        elif re.fullmatch(r"[Ff][Ll][Oo][Cc][Kk]\d+", ssid):
            numeric[ssid] += count
        else:
            other[ssid] += count

    return {
        "bare":            (len(bare),            sum(bare.values())),
        "dash_6hex":       (len(dash_6hex),        sum(dash_6hex.values())),
        "upper_dash_6hex": (len(upper_dash_6hex),  sum(upper_dash_6hex.values())),
        "dash_4hex":       (len(dash_4hex),        sum(dash_4hex.values())),
        "numeric":         (len(numeric),           sum(numeric.values())),
        "other":           (len(other),             sum(other.values())),
    }


# ── Marker-based replacement ──────────────────────────────────────────────────
def replace_between_markers(content: str, start_marker: str, end_marker: str, new_inner: str) -> str:
    """Replace everything between start_marker and end_marker (exclusive) with new_inner."""
    pattern = re.compile(
        re.escape(start_marker) + r".*?" + re.escape(end_marker),
        re.DOTALL,
    )
    replacement = f"{start_marker}\n{new_inner}\n{end_marker}"
    new_content, count = pattern.subn(replacement, content)
    if count == 0:
        raise ValueError(
            f"Marker pair not found in file:\n  start: {start_marker!r}\n  end:   {end_marker!r}\n"
            "Add the marker comments to the file before running this script."
        )
    return new_content


# ── README generators ─────────────────────────────────────────────────────────
def readme_top10_block(top10, total_records: int, unique_count: int, oui_file_count: int) -> str:
    rows = "\n".join(
        f"| {i + 1} | `{ssid}` | {count:,} |"
        for i, (ssid, count) in enumerate(top10)
    )
    return (
        "| # | SSID | Occurrences |\n"
        "|---|------|-------------|\n"
        f"{rows}\n\n"
        f"*Computed from {total_records:,} SSID-bearing records ({unique_count:,} unique values) "
        f"across all {oui_file_count} OUI files in `data/by_oui/`. "
        "Stats update automatically after each scan.*"
    )


def readme_patterns_block(
    flock_total_unique: int,
    flock_total_records: int,
    patterns: dict,
) -> str:
    bare_u, bare_r         = patterns["bare"]
    d6_u,   d6_r           = patterns["dash_6hex"]
    ud6_u,  ud6_r          = patterns["upper_dash_6hex"]
    d4_u,   d4_r           = patterns["dash_4hex"]
    num_u,  num_r          = patterns["numeric"]
    oth_u,  oth_r          = patterns["other"]

    d4_ex = "`Flock-6361`" if d4_r else "—"
    num_ex = "`Flock001`, `Flock003`" if num_r else "—"

    rows = (
        f"| `Flock` | {bare_u:,} | {bare_r:,} | Bare name — fully configured / deployed cameras |\n"
        f"| `Flock-XXXXXX` | {d6_u:,} | {d6_r:,} | Mixed-case with 6-char uppercase hex suffix |\n"
        f"| `FLOCK-XXXXXX` | {ud6_u:,} | {ud6_r:,} | All-caps variant with 6-char hex suffix |\n"
        f"| `Flock-XXXX` | {d4_u:,} | {d4_r:,} | Shorter 4-char hex suffix ({d4_ex}) |\n"
        f"| `FlockXXX` | {num_u:,} | {num_r:,} | Numeric suffix, no dash ({num_ex}) |"
    )
    if oth_r:
        rows += f"\n| Other | {oth_u:,} | {oth_r:,} | Other / non-standard patterns |"

    return (
        f"Filtering for only `Flock*`-prefixed SSIDs yields **{flock_total_unique:,} unique variants** "
        f"across **{flock_total_records:,} total records**. These fall into five distinct patterns:\n\n"
        "| Pattern | Unique SSIDs | Records | Description |\n"
        "|---------|-------------|---------|-------------|\n"
        f"{rows}\n\n"
        "**The `Flock-XXXXXX` / `FLOCK-XXXXXX` naming convention is consistent with camera provisioning SSIDs** "
        "— each device appears to broadcast a unique hex identifier (likely derived from its MAC address) "
        "before being claimed and configured through the Flock Safety platform. Once provisioned, the SSID "
        "collapses to the bare `Flock` name.\n\n"
        "> This pattern is a strong secondary confirmation signal: observing a `Flock-XXXXXX` SSID on a "
        "matching OUI prefix is highly indicative of an unconfigured or recently factory-reset Flock Safety camera."
    )


# ── HTML generators ───────────────────────────────────────────────────────────
def html_top10_rows(top10) -> str:
    rows = []
    for i, (ssid, count) in enumerate(top10):
        safe = html_lib.escape(ssid)
        rows.append(
            f'                    <tr>'
            f'<td>{i + 1}</td>'
            f'<td><code>{safe}</code></td>'
            f'<td style="color:var(--accent);font-weight:700;">{count:,}</td>'
            f'</tr>'
        )
    return "\n".join(rows)


def html_top10_stats(total_records: int, unique_count: int, oui_file_count: int) -> str:
    return (
        '        <p style="margin-top:0.75rem;font-size:0.8rem;color:var(--text-muted);">\n'
        f'            Computed from {total_records:,} SSID-bearing records ({unique_count:,} unique values) '
        f'across all {oui_file_count} OUI files in\n'
        '            <code>data/by_oui/</code>. Stats update automatically after each scan.\n'
        '        </p>'
    )


def html_patterns_summary(flock_total_unique: int, flock_total_records: int) -> str:
    return (
        '        <p>\n'
        '            Filtering the WiGLE dataset to only <code>Flock*</code>-prefixed SSIDs reveals\n'
        f'            <strong>{flock_total_unique:,} unique variants</strong> across '
        f'<strong>{flock_total_records:,} total records</strong>,\n'
        '            falling into five distinct naming patterns:\n'
        '        </p>'
    )


def html_patterns_rows(patterns: dict) -> str:
    bare_u, bare_r         = patterns["bare"]
    d6_u,   d6_r           = patterns["dash_6hex"]
    ud6_u,  ud6_r          = patterns["upper_dash_6hex"]
    d4_u,   d4_r           = patterns["dash_4hex"]
    num_u,  num_r          = patterns["numeric"]

    green  = "var(--green)"
    accent = "var(--accent)"
    muted  = "var(--text-muted)"

    def row(pat: str, u: int, r: int, desc: str, color: str) -> str:
        return (
            f"                    <tr>\n"
            f"                        <td><code>{pat}</code></td>\n"
            f"                        <td style=\"color:{color};font-weight:700;\">{u:,}</td>\n"
            f"                        <td style=\"color:{color};font-weight:700;\">{r:,}</td>\n"
            f"                        <td style=\"color:{muted};\">{desc}</td>\n"
            f"                    </tr>"
        )

    return "\n".join([
        row("Flock",        bare_u, bare_r,
            "Bare name — fully configured / deployed cameras", accent),
        row("Flock-XXXXXX", d6_u,   d6_r,
            "Mixed-case with 6-char uppercase hex suffix (e.g. <code>Flock-7EBB9D</code>)", green),
        row("FLOCK-XXXXXX", ud6_u,  ud6_r,
            "All-caps variant with 6-char hex suffix (e.g. <code>FLOCK-215CB4</code>)", green),
        row("Flock-XXXX",   d4_u,   d4_r,
            "Shorter 4-char hex suffix (<code>Flock-6361</code>)", muted),
        row("FlockXXX",     num_u,  num_r,
            "Numeric suffix, no dash (<code>Flock001</code>, <code>Flock003</code>)", muted),
    ])


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    print("Collecting SSID data from data/by_oui/...")
    all_ssids, flock_ssids, total_records, oui_file_count = collect_ssid_data()

    top10             = all_ssids.most_common(10)
    unique_count      = len(all_ssids)
    flock_total_unique  = len(flock_ssids)
    flock_total_records = sum(flock_ssids.values())
    patterns          = group_flock_patterns(flock_ssids)

    print(f"  Total SSID-bearing records : {total_records:,}")
    print(f"  Unique SSIDs               : {unique_count:,}")
    print(f"  Flock* unique / records    : {flock_total_unique:,} / {flock_total_records:,}")
    print(f"  OUI CSV files processed    : {oui_file_count}")

    # ── Update README.md ──────────────────────────────────────────────────────
    print("\nUpdating README.md ...")
    readme_content = README.read_text(encoding="utf-8")

    readme_content = replace_between_markers(
        readme_content,
        "<!-- SSID_TOP10_START -->",
        "<!-- SSID_TOP10_END -->",
        readme_top10_block(top10, total_records, unique_count, oui_file_count),
    )
    readme_content = replace_between_markers(
        readme_content,
        "<!-- SSID_PATTERNS_START -->",
        "<!-- SSID_PATTERNS_END -->",
        readme_patterns_block(flock_total_unique, flock_total_records, patterns),
    )

    README.write_text(readme_content, encoding="utf-8")
    print("  README.md updated.")

    # ── Update docs/index.html ────────────────────────────────────────────────
    print("\nUpdating docs/index.html ...")
    html_content = INDEX_HTML.read_text(encoding="utf-8")

    html_content = replace_between_markers(
        html_content,
        "<!-- SSID_TOP10_ROWS_START -->",
        "<!-- SSID_TOP10_ROWS_END -->",
        html_top10_rows(top10),
    )
    html_content = replace_between_markers(
        html_content,
        "<!-- SSID_TOP10_STATS_START -->",
        "<!-- SSID_TOP10_STATS_END -->",
        html_top10_stats(total_records, unique_count, oui_file_count),
    )
    html_content = replace_between_markers(
        html_content,
        "<!-- SSID_PATTERNS_SUMMARY_START -->",
        "<!-- SSID_PATTERNS_SUMMARY_END -->",
        html_patterns_summary(flock_total_unique, flock_total_records),
    )
    html_content = replace_between_markers(
        html_content,
        "<!-- SSID_PATTERNS_ROWS_START -->",
        "<!-- SSID_PATTERNS_ROWS_END -->",
        html_patterns_rows(patterns),
    )

    INDEX_HTML.write_text(html_content, encoding="utf-8")
    print("  docs/index.html updated.")

    print("\nDone.")


if __name__ == "__main__":
    main()
