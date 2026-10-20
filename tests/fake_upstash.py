"""In-memory Upstash REST transport: executes the commands Kv sends, with TTLs on a fake clock."""
import json


class FakeUpstash:
    def __init__(self):
        self.data = {}      # key -> str | list | dict
        self.expires = {}   # key -> absolute fake-clock seconds
        self.clock = 0.0
        self.calls = []

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
        cmd = json.loads(body)
        self.calls.append(cmd)
        op, args = cmd[0].upper(), cmd[1:]
        d = self.data
        if op == "GET":
            res = d.get(args[0])
        elif op == "SET":
            d[args[0]] = args[1]
            self.expires.pop(args[0], None)
            if len(args) == 4 and args[2].upper() == "EX":
                self.expires[args[0]] = self.clock + int(args[3])
            res = "OK"
        elif op == "INCR":
            d[args[0]] = str(int(d.get(args[0], "0")) + 1)
            res = int(d[args[0]])
        elif op == "EXPIRE":
            res = 1 if args[0] in d else 0
            if res:
                self.expires[args[0]] = self.clock + int(args[1])
        elif op == "DEL":
            res = 0
            for k in args:
                res += 1 if d.pop(k, None) is not None else 0
                self.expires.pop(k, None)
        elif op == "RPUSH":
            d.setdefault(args[0], []).append(args[1])
            res = len(d[args[0]])
        elif op == "LRANGE":
            lst = d.get(args[0], [])
            stop = int(args[2])
            res = lst[int(args[1]):(None if stop == -1 else stop + 1)]
        elif op == "HSET":
            h = d.setdefault(args[0], {})
            res = 0 if args[1] in h else 1
            h[args[1]] = args[2]
        elif op == "HGETALL":
            res = [x for kv in d.get(args[0], {}).items() for x in kv]
        else:
            return 200, {}, json.dumps({"error": "ERR unknown command " + op}).encode()
        return 200, {}, json.dumps({"result": res}).encode()
