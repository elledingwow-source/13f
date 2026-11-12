"""Update a local quarterly report. Use --scheduled to enforce the filing window."""
import argparse
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
STAMP = re.compile(r"生成于 \d{4}-\d{2}-\d{2} \d{2}:\d{2} 北京时间")


def report_body(html):
    html = re.sub(r"(<title>13F 持仓监控 — )\d{4}-\d{2}-\d{2}(</title>)", r"\1\2", html)
    return STAMP.sub("", html)


def filing_window(today):
    start_month = 3 * ((today.month - 1) // 3) + 1
    end = date(today.year, start_month, 1) - timedelta(days=1)
    return f"{end.year}Q{(end.month + 2) // 3}", (today - end).days


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quarter", type=str.upper, help="Example: 2026Q3")
    mode.add_argument("--scheduled", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    args = parser.parse_args()

    quarter, age = filing_window(datetime.now(ZoneInfo("America/New_York")).date())
    if args.scheduled and not 40 <= age <= 45:
        print("Outside the filing window")
        return
    quarter = args.quarter or quarter
    if not re.fullmatch(r"[1-9]\d{3}Q[1-4]", quarter):
        parser.error("--quarter must use YYYYQ1 through YYYYQ4")
    env = os.environ.copy()
    if not env.get("SEC_USER_AGENT"):
        if args.scheduled or not sys.stdin.isatty():
            parser.error("Set SEC_USER_AGENT to your name and contact email")
        env["SEC_USER_AGENT"] = input("SEC contact (name and email): ").strip()

    target = args.output_dir.resolve() / f"report-{quarter}.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".13f-", dir=target.parent) as directory:
        candidate = Path(directory) / target.name
        env["SEC_13F_DB"] = str(Path(directory) / "monitor.db")
        subprocess.run(
            [sys.executable, str(ROOT / "monitor.py"), "--quarter", quarter,
             "--strict", "--output", str(candidate)],
            cwd=ROOT, env=env, check=True,
        )
        if target.exists() and report_body(target.read_text(encoding="utf-8")) == report_body(
            candidate.read_text(encoding="utf-8")
        ):
            print(f"No report changes: {target}")
            return
        candidate.replace(target)
    print(f"Updated: {target}")


if __name__ == "__main__":
    main()
