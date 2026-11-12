from branchlake import Lake

INITIAL = [
    ["sample", "a", {"station": "east", "v": 10}],
    ["sample", "b", {"station": "west", "v": 20}],
    ["station", "east", {"region": "north"}],
]
CORRECTION = [
    ["sample", "a", {"station": "east", "v": 11}],
    ["sample", "b", None],
    ["sample", "c", {"station": "west", "v": 30}],
    ["station", "east", {"region": "south"}],
]
SIDE = [
    ["sample", "a", {"station": "east", "v": 12}],
    ["sample", "d", {"station": "west", "v": 40}],
]
MERGE = SIDE + [["station", "east", {"region": "merged"}]]
LATE = [["sample", "z", {"station": "east", "v": 99}]]


def replay():
    lake = Lake()
    first = lake.publish("main", 0, "initial", INITIAL)
    lake.fork("side", first["snapshot"])
    corrected = lake.publish("main", 1, "correct", CORRECTION)
    side = lake.publish("side", 0, "side", SIDE)
    before = lake.objects.reads
    rewritten = lake.publish("main", 2, "rewrite", [], rewrite=True)
    rewrite_reads = lake.objects.reads - before
    merged = lake.publish("main", 3, "merge", MERGE, merge_parent=side["snapshot"])
    reset = lake.reset("main", 4, "back", first["snapshot"])
    forward = lake.reset("main", 5, "forward", merged["snapshot"])
    replayed = lake.publish("main", 0, "initial", INITIAL)
    before_ids = set(lake.objects.ids())
    try:
        lake.publish("main", 6, "late", LATE, fail_after_object=True)
    except RuntimeError as exc:
        assert str(exc) == "AFTER_OBJECT"
    orphan_ids = sorted(set(lake.objects.ids()) - before_ids)
    unpublished = lake.read()
    published = lake.publish("main", 6, "late", LATE)
    entries = [
        ("initial", first), ("correct", corrected), ("rewrite", rewritten),
        ("merge", merged), ("back", reset), ("forward", forward), ("late", published),
    ]
    previous = 0
    epochs = []
    for label, receipt in entries:
        sid = receipt["snapshot"]
        epochs.append(dict(receipt, label=label, delta=lake.delta(previous, sid),
                           rows=lake.rows(sid)))
        previous = sid
    trace = {
        "epochs": epochs,
        "side": dict(side, rows=lake.rows(side["snapshot"])),
        "merge_parents": lake.manifest(merged["snapshot"])["parents"],
        "replayed_initial": replayed,
        "current": lake.read(),
        "rewrite_payload_reads": rewrite_reads,
        "orphan_ids": orphan_ids,
        "unpublished": unpublished,
        "late_retry": published,
    }
    return lake, trace
