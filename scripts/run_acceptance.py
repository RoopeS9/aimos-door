"""Execute every acceptance case and print an advisor-friendly complete table."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    output = ROOT / "data" / "verification"
    output.mkdir(parents=True, exist_ok=True)
    report = output / "acceptance.xml"
    if report.exists():
        report.unlink()
    ids = re.findall(r"^\| ([A-N]\d{2}) \| (\d) \|", (ROOT / "ACCEPTANCE.md").read_text(), re.M)
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    run = subprocess.run([sys.executable, "-m", "pytest", "tests/acceptance", "-q", "--tb=short", f"--junitxml={report}"], cwd=ROOT, env=env)
    # Real runs always require every ACCEPTANCE row. Isolated report-format
    # callers may copy cases without copying the suite; assess the rows their
    # synthetic report actually represents rather than unrelated future cases.
    suite_available = (ROOT / "tests" / "acceptance").is_dir()
    results = {}
    if report.exists():
        for case in ET.parse(report).iter("testcase"):
            match = re.search(r"(?:test_)?([A-N]\d{2})(?:$|_)", case.attrib.get("name", ""))
            if match:
                case_id = match[1]
                status = "FAIL" if any(case.find(k) is not None for k in ("failure", "error", "skipped")) else "PASS"
                # Multiple implementations of one ID are a test-inventory failure.
                results[case_id] = status if case_id not in results else "FAIL"
    table = "| ID | Milestone | Result |\n| --- | --- | --- |\n" + "\n".join(f"| {case_id} | M{milestone} | {results.get(case_id, 'FAIL')} |" for case_id, milestone in ids)
    print("\n" + table, flush=True)
    (output / "acceptance-table.md").write_text(table + "\n")
    (output / "acceptance-results.json").write_text(json.dumps({case_id: results.get(case_id, "FAIL") for case_id, _ in ids}, indent=2) + "\n")
    status_path = ROOT / "STATUS.md"
    if status_path.exists():
        status = status_path.read_text()
        heading = "## Acceptance table"
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        section = f"{heading}\n\nLast executed: {timestamp}. Missing, errored, or skipped cases count as FAIL.\n\n{table}\n"
        if heading in status:
            start = status.index(heading)
            following = status.find("\n## ", start + len(heading))
            status = status[:start] + section + (status[following:] if following != -1 else "")
        else:
            status += "\n" + section
        lines = []
        for line in status.splitlines():
            parts = line.split("|")
            if len(parts) == 6 and re.fullmatch(r"M[1-4]", parts[1].strip()):
                milestone = parts[1].strip()[1:]
                milestone_ids = [case_id for case_id, m in ids if m == milestone]
                passed = sum(results.get(case_id) == "PASS" for case_id in milestone_ids)
                parts[3] = f" {passed}/{len(milestone_ids)} PASS "
                line = "|".join(parts)
            lines.append(line)
        status = "\n".join(lines) + "\n"
        passed = sum(results.get(case_id) == "PASS" for case_id, _ in ids)
        status = re.sub(r"Last `make acceptance`: \d+ PASS, \d+ FAIL", f"Last `make acceptance`: {passed} PASS, {len(ids) - passed} FAIL", status)
        status_path.write_text(status)
    required = ids if suite_available else [row for row in ids if row[0] in results]
    return 0 if run.returncode == 0 and all(results.get(case_id) == "PASS" for case_id, _ in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
