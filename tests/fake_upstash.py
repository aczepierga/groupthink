"""In-memory Upstash REST transport: executes the commands Kv sends, with TTLs on a fake clock.

The clock starts at the real unix time so EXPIREAT deadlines (absolute unix seconds) are comparable.
POST <url>/multi-exec takes a 2-D JSON array and answers [{"result": ...} | {"error": ...}, ...].
"""
import fnmatch
import json
import time


class FakeUpstash:
    def __init__(self):
        self.data = {}      # key -> str | list | dict
        self.expires = {}   # key -> absolute unix seconds on the fake clock
        self.clock = float(int(time.time()))
        self.calls = []     # every command, flattened, in execution order
        self.requests = []  # (url, parsed body) per HTTP request

    def advance(self, seconds):
        self.clock += seconds

    def ttl(self, key):
        return self.expires[key] - self.clock if key in self.expires else None

    def keys(self):
        self._sweep()
        return list(self.data)

    def _sweep(self):
        for k in [k for k, t in self.expires.items() if t <= self.clock]:
            self.data.pop(k, None)
            self.expires.pop(k, None)

    def __call__(self, method, url, headers, body, timeout):
        self._sweep()
        parsed = json.loads(body)
        self.requests.append((url, parsed))
        if url.endswith("/multi-exec"):
            out = []
            for cmd in parsed:
                self.calls.append(cmd)
                try:
                    out.append({"result": self._exec(cmd)})
                except ValueError as exc:
                    out.append({"error": str(exc)})
            return 200, {}, json.dumps(out).encode()
        self.calls.append(parsed)
        try:
            return 200, {}, json.dumps({"result": self._exec(parsed)}).encode()
        except ValueError as exc:
            return 200, {}, json.dumps({"error": str(exc)}).encode()

    def _exec(self, cmd):
        op, args = cmd[0].upper(), cmd[1:]
        d = self.data
        if op == "GET":
            return d.get(args[0])
        if op == "SET":
            d[args[0]] = args[1]
            self.expires.pop(args[0], None)
            if len(args) == 4 and args[2].upper() == "EX":
                self.expires[args[0]] = self.clock + int(args[3])
            return "OK"
        if op == "INCR":
            d[args[0]] = str(int(d.get(args[0], "0")) + 1)
            return int(d[args[0]])
        if op == "DECR":
            d[args[0]] = str(int(d.get(args[0], "0")) - 1)
            return int(d[args[0]])
        if op in ("INCRBY", "DECRBY"):
            step = int(args[1]) * (1 if op == "INCRBY" else -1)
            d[args[0]] = str(int(d.get(args[0], "0")) + step)
            return int(d[args[0]])
        if op == "HLEN":
            return len(d.get(args[0], {}))
        if op == "HDEL":
            return 1 if d.get(args[0], {}).pop(args[1], None) is not None else 0
        if op in ("EXPIRE", "EXPIREAT"):
            if args[0] not in d:
                return 0
            self.expires[args[0]] = (self.clock + int(args[1])) if op == "EXPIRE" else int(args[1])
            return 1
        if op == "DEL":
            n = 0
            for k in args:
                n += 1 if d.pop(k, None) is not None else 0
                self.expires.pop(k, None)
            return n
        if op == "RPUSH":
            d.setdefault(args[0], []).append(args[1])
            return len(d[args[0]])
        if op == "LRANGE":
            lst = d.get(args[0], [])
            stop = int(args[2])
            return lst[int(args[1]):(None if stop == -1 else stop + 1)]
        if op == "HSET":
            h = d.setdefault(args[0], {})
            new = 0 if args[1] in h else 1
            h[args[1]] = args[2]
            return new
        if op == "HGETALL":
            return [x for kv in d.get(args[0], {}).items() for x in kv]
        if op == "SCAN":
            pattern = args[args.index("MATCH") + 1]
            return ["0", [k for k in d if fnmatch.fnmatchcase(k, pattern)]]
        raise ValueError("ERR unknown command " + op)
