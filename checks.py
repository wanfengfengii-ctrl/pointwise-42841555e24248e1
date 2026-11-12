import json
import pathlib
import re
import sys
import sqlite3

from branchlake import Conflict, Lake
from samples import CORRECTION, INITIAL, replay


def normal():
    lake, trace = replay()
    epochs = trace["epochs"]
    assert [x["epoch"] for x in epochs] == [1, 2, 3, 4, 5, 6, 7]
    assert [x["snapshot"] for x in epochs] == [1, 2, 4, 5, 1, 5, 6]
    assert epochs[1]["delta"] == [
        ["sample", "a", {"station": "east", "v": 10}, {"station": "east", "v": 11}],
        ["sample", "b", {"station": "west", "v": 20}, None],
        ["sample", "c", None, {"station": "west", "v": 30}],
        ["station", "east", {"region": "north"}, {"region": "south"}],
    ]
    assert epochs[2]["delta"] == []
    assert epochs[1]["rows"] == epochs[2]["rows"]
    assert epochs[3]["rows"] == [
        ["sample", "a", {"station": "east", "v": 12}],
        ["sample", "c", {"station": "west", "v": 30}],
        ["sample", "d", {"station": "west", "v": 40}],
        ["station", "east", {"region": "merged"}],
    ]
    assert trace["merge_parents"] == [4, 3]
    assert trace["side"]["rows"] == [
        ["sample", "a", {"station": "east", "v": 12}],
        ["sample", "b", {"station": "west", "v": 20}],
        ["sample", "d", {"station": "west", "v": 40}],
        ["station", "east", {"region": "north"}],
    ]
    assert epochs[4]["rows"] == INITIAL
    assert epochs[5]["rows"] == epochs[3]["rows"]
    assert epochs[4]["delta"] and epochs[5]["delta"]
    assert trace["replayed_initial"] == {"ref": "main", "epoch": 1, "snapshot": 1}
    assert trace["unpublished"]["epoch"] == 6
    assert not any(r[1] == "z" for r in trace["unpublished"]["rows"])
    assert len(trace["orphan_ids"]) == 1
    assert trace["late_retry"] == {"ref": "main", "epoch": 7, "snapshot": 6}
    assert trace["current"]["rows"] == epochs[3]["rows"][:-1] + [
        ["sample", "z", {"station": "east", "v": 99}],
        ["station", "east", {"region": "merged"}],
    ]
    head = lake.read()
    for action, marker in [
        (lambda: lake.publish("main", 0, "initial", CORRECTION), "REQUEST_CONFLICT"),
        (lambda: lake.publish("main", 0, "stale", []), "STALE_EPOCH"),
    ]:
        try:
            action()
        except Conflict as exc:
            assert str(exc) == marker
        else:
            raise AssertionError(marker)
        assert lake.read() == head
    try:
        lake.publish("main", 7, "invalid", [["sample", "a", {}], ["sample", "a", None]])
    except ValueError as exc:
        assert str(exc) == "DUPLICATE_KEY"
    else:
        raise AssertionError("INVALID_BATCH_ACCEPTED")
    assert lake.read() == head
    # Reopen both SQLite images without relying on a filesystem.
    reopened = Lake()
    reopened.db.close()
    reopened.objects.db.close()
    reopened.db = sqlite3.connect(":memory:", isolation_level=None)
    reopened.objects.db = sqlite3.connect(":memory:", isolation_level=None)
    reopened.db.executescript("\n".join(lake.db.iterdump()))
    reopened.objects.db.executescript("\n".join(lake.objects.db.iterdump()))
    assert reopened.read() == head
    assert reopened.publish("main", 0, "initial", INITIAL) == trace["replayed_initial"]
    print("NORMAL_REGRESSION_OK")
    return trace


def design():
    trace = normal()
    text = pathlib.Path("DESIGN.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.S)
    evidence = [json.loads(block) for block in blocks]
    evidence = [x for x in evidence if isinstance(x, dict) and "baseline_trace" in x]
    assert len(evidence) == 1, "ONE_BASELINE_EVIDENCE_BLOCK"
    assert evidence[0]["baseline_trace"] == trace, "BASELINE_TRACE_MISMATCH"
    for token in ("A", "B", "C", "D", "E", "F"):
        decision = evidence[0]["decisions"][token]
        assert isinstance(decision, str) and decision.strip(), "MISSING_DESIGN_DECISION"
    refs = re.findall(r"(branchlake\.py|samples\.py|checks\.py):L(\d+)", text)
    assert refs, "MISSING_SOURCE_LOCATIONS"
    for file, line in refs:
        assert 1 <= int(line) <= len(pathlib.Path(file).read_text(encoding="utf-8").splitlines())
    print("DESIGN_BASELINE_EVIDENCE_OK")
    print("Evidence equality and line ranges checked; protocol correctness requires review.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else "normal"
    if mode == "normal":
        normal()
    elif mode == "trace":
        print(json.dumps(replay()[1], ensure_ascii=False, sort_keys=True, indent=2))
    elif mode == "demo":
        trace = normal()
        for item in trace["epochs"]:
            print(json.dumps({k: item[k] for k in ("label", "epoch", "snapshot", "delta")},
                             ensure_ascii=False, sort_keys=True))
        print(json.dumps({"replayed_initial": trace["replayed_initial"],
                          "unpublished": trace["unpublished"], "orphan_ids": trace["orphan_ids"]},
                         ensure_ascii=False, sort_keys=True))
    elif mode == "design":
        design()
    else:
        raise SystemExit("normal | trace | demo | design")
