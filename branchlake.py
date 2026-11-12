"""Reference branched lake. No subscription or garbage collector is implemented."""
import hashlib
import json
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)


class Conflict(Exception):
    pass


class Objects:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.execute("CREATE TABLE IF NOT EXISTS blobs(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.reads = 0

    def put(self, rows):
        body = encode(rows)
        oid = hashlib.sha256(body.encode("utf-8")).hexdigest()
        self.db.execute("INSERT OR IGNORE INTO blobs VALUES(?,?)", (oid, body))
        if self.db.execute("SELECT body FROM blobs WHERE id=?", (oid,)).fetchone()[0] != body:
            raise Conflict("DIGEST_COLLISION")
        return oid

    def get(self, oid):
        self.reads += 1
        row = self.db.execute("SELECT body FROM blobs WHERE id=?", (oid,)).fetchone()
        if row is None:
            raise Conflict("MISSING_OBJECT")
        return json.loads(row[0])

    def ids(self):
        return [r[0] for r in self.db.execute("SELECT id FROM blobs ORDER BY id")]


class Lake:
    def __init__(self, catalog=":memory:", objects=":memory:"):
        self.objects = Objects(objects)
        self.db = sqlite3.connect(catalog, isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS snapshots(
            sid INTEGER PRIMARY KEY AUTOINCREMENT, parents TEXT NOT NULL, manifest TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS refs(
            name TEXT PRIMARY KEY, epoch INTEGER NOT NULL, sid INTEGER NOT NULL REFERENCES snapshots);
        CREATE TABLE IF NOT EXISTS requests(
            request TEXT PRIMARY KEY, binding TEXT NOT NULL, receipt TEXT NOT NULL);
        INSERT OR IGNORE INTO snapshots VALUES(0,'[]','[]');
        INSERT OR IGNORE INTO refs VALUES('main',0,0);
        """)

    def ref(self, name):
        row = self.db.execute("SELECT epoch,sid FROM refs WHERE name=?", (name,)).fetchone()
        if row is None:
            raise Conflict("UNKNOWN_REF")
        return {"ref": name, "epoch": row[0], "snapshot": row[1]}

    def manifest(self, sid):
        row = self.db.execute("SELECT parents,manifest FROM snapshots WHERE sid=?", (sid,)).fetchone()
        if row is None:
            raise Conflict("UNKNOWN_SNAPSHOT")
        return {"parents": json.loads(row[0]), "objects": json.loads(row[1])}

    def fork(self, name, sid):
        self.manifest(sid)
        self.db.execute("INSERT INTO refs VALUES(?,0,?)", (name, sid))

    def rows(self, sid):
        state = {}
        for oid in self.manifest(sid)["objects"]:
            for table, key, value in self.objects.get(oid):
                if value is None:
                    state.pop((table, key), None)
                else:
                    state[table, key] = value
        return [[t, k, state[t, k]] for t, k in sorted(state)]

    def read(self, name="main"):
        fixed = self.ref(name)
        return dict(fixed, rows=self.rows(fixed["snapshot"]))

    def receipt(self, request, binding):
        row = self.db.execute("SELECT binding,receipt FROM requests WHERE request=?", (request,)).fetchone()
        if row is None:
            return None
        if row[0] != binding:
            raise Conflict("REQUEST_CONFLICT")
        return json.loads(row[1])

    def _install(self, name, expected, request, binding, target=None, prepared=None):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            prior = self.receipt(request, binding)
            if prior is not None:
                self.db.execute("COMMIT")
                return prior
            head = self.ref(name)
            if head["epoch"] != expected:
                raise Conflict("STALE_EPOCH")
            if prepared is not None:
                parents, objects = prepared
                cur = self.db.execute("INSERT INTO snapshots(parents,manifest) VALUES(?,?)",
                                      (encode(parents), encode(objects)))
                target = cur.lastrowid
            result = {"ref": name, "epoch": expected + 1, "snapshot": target}
            self.db.execute("UPDATE refs SET epoch=?,sid=? WHERE name=?",
                            (result["epoch"], target, name))
            self.db.execute("INSERT INTO requests VALUES(?,?,?)", (request, binding, encode(result)))
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def publish(self, name, expected, request, rows, merge_parent=None,
                rewrite=False, fail_after_object=False):
        if not isinstance(request, str) or not request:
            raise ValueError("REQUEST")
        if type(expected) is not int or expected < 0 or type(rewrite) is not bool:
            raise ValueError("OPTIONS")
        seen = set()
        if not isinstance(rows, list):
            raise ValueError("ROWS")
        for row in rows:
            if not isinstance(row, list) or len(row) != 3:
                raise ValueError("ROW")
            t, k, v = row
            if not isinstance(t, str) or not t or not isinstance(k, str) or not k:
                raise ValueError("KEY")
            if v is not None and not isinstance(v, dict):
                raise ValueError("VALUE")
            if (t, k) in seen:
                raise ValueError("DUPLICATE_KEY")
            seen.add((t, k))
        binding = encode(["publish", name, expected, rows, merge_parent, rewrite])
        prior = self.receipt(request, binding)
        if prior is not None:
            return prior
        head = self.ref(name)
        if head["epoch"] != expected:
            raise Conflict("STALE_EPOCH")
        old = self.manifest(head["snapshot"])
        parents = [head["snapshot"]]
        if merge_parent is not None:
            self.manifest(merge_parent)
            parents.append(merge_parent)
        if rewrite:
            state = {(t, k): v for t, k, v in self.rows(head["snapshot"])}
            for t, k, v in rows:
                if v is None:
                    state.pop((t, k), None)
                else:
                    state[t, k] = v
            payload = [[t, k, state[t, k]] for t, k in sorted(state)]
        else:
            payload = rows
        oid = self.objects.put(payload)
        if fail_after_object:
            raise RuntimeError("AFTER_OBJECT")
        manifest = [oid] if rewrite else old["objects"] + [oid]
        return self._install(name, expected, request, binding, prepared=(parents, manifest))

    def reset(self, name, expected, request, sid):
        binding = encode(["reset", name, expected, sid])
        prior = self.receipt(request, binding)
        if prior is not None:
            return prior
        self.manifest(sid)
        return self._install(name, expected, request, binding, target=sid)

    def delta(self, before, after):
        left = {(t, k): v for t, k, v in self.rows(before)}
        right = {(t, k): v for t, k, v in self.rows(after)}
        return [[t, k, left.get((t, k)), right.get((t, k))]
                for t, k in sorted(left.keys() | right.keys())
                if left.get((t, k)) != right.get((t, k))]
