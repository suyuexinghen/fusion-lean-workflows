"""Replay a small, synthetic data-contract experiment. No files or network required."""
from argparse import ArgumentParser
from decimal import Decimal
from io import StringIO
import csv
import json


MISSING = {"A": frozenset({""}), "B": frozenset({"NR"})}
INPUTS = {
    "A": "day,value\n1,0\n2,12\n3,\n",
    "B": "day,value\n1,4\n2,0\n3,18\n4,NR\n",
}
# Frozen from the example supplier contracts, independently of both parsers.
EXPECTED = {"A": (3, 2, Decimal(6)), "B": (4, 3, Decimal(22) / 3)}


def read_metric(text, missing_tokens):
    rows = list(csv.DictReader(StringIO(text)))
    values = [Decimal(row["value"]) for row in rows if row["value"] not in missing_tokens]
    return len(rows), len(values), sum(values) / len(values) if values else None


def evaluate(supplier, include_zero_as_missing):
    tokens = MISSING[supplier] | {"0"} if include_zero_as_missing else MISSING[supplier]
    actual = read_metric(INPUTS[supplier], tokens)
    expected = EXPECTED[supplier]
    return {"actual": [str(v) for v in actual], "expected": [str(v) for v in expected],
            "result": "PASS" if actual == expected else "FAIL"}


def compare_gates():
    # Each gate sees the exact same outputs. The expected decisions come from the contract.
    decisions = []
    for supplier in INPUTS:
        for behavior, faulty in (("correct", False), ("drops_zero", True)):
            output = evaluate(supplier, faulty)
            actual = output["actual"]
            structure_pass = len(actual) == 3 and all(Decimal(v).is_finite() for v in actual)
            semantic_pass = structure_pass and output["result"] == "PASS"
            decisions.append({"supplier": supplier, "implementation": behavior, "expected_accept": not faulty,
                              "old_gate_accept": structure_pass, "new_gate_accept": semantic_pass})
    print(json.dumps(decisions, indent=2))
    return int(not all(d["old_gate_accept"] and d["new_gate_accept"] == d["expected_accept"] for d in decisions))


def main():
    parser = ArgumentParser(description=__doc__)
    select = parser.add_mutually_exclusive_group()
    select.add_argument("--compare-gates", action="store_true", help="compare structure-only and data-contract checks")
    select.add_argument("--rule", choices=("legacy", "candidate", "both"), default="both")
    args = parser.parse_args()
    if args.compare_gates:
        return compare_gates()
    selected = ("legacy", "candidate") if args.rule == "both" else (args.rule,)
    result = {rule: {supplier: evaluate(supplier, rule == "legacy") for supplier in INPUTS}
              for rule in selected}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.rule == "both":
        # CI for the teaching fixture: the old rule must fail for the intended reason.
        return int(not all(result["legacy"][s]["result"] == "FAIL"
                           and result["candidate"][s]["result"] == "PASS" for s in INPUTS))
    return int(any(row["result"] != "PASS" for row in result[args.rule].values()))


if __name__ == "__main__":
    raise SystemExit(main())
