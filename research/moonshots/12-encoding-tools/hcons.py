"""Prototype (moonshot 12): position-independent, hash-consed encoding of diffexec call inputs.

Every sqlglot Expr reachable from a call input is replaced (pickle persistent id) by
    ("R", root_node_id, k, path, sig_id)  first reference to root k
    ("E", k, path)                        later references to root k
root_node_id: the root's id in a GLOBAL content-addressed node table (hash-consing: a node
record = (class, ((arg_key, value | child node id | list), ...), comments, has_type, meta,
_hash); children are node ids, so identical subtrees across all calls / functions are stored
once). k: ordinal of that root OBJECT within this input (distinct-but-equal trees stay
distinct). sig_id: id in a global table of type signatures: one entry per non-None `_type`
slot of the tree, in build order, giving the identity of that DataType object:
    ("new", node id)       a parentless DataType first seen here (gets the next ordinal)
    ("old", ordinal)       the same object as an earlier parentless DataType of this input
    ("node", k, path)      a node of this same tree (e.g. Cast.to)
Types are patched after the whole input is loaded.

Exactness: a tree is encodable only if every child's parent/arg_key/index is canonical and no
node / list / dict is reachable twice inside it (otherwise: raw-pickle fallback). The round
trip is checked with serial.digest_cmp (the effect digest diffexec itself compares).
"""
from __future__ import annotations

import enum
import importlib
import io
import pickle

from diffexec import serial


class NotCanonical(Exception):
    pass


_LIT = (str, int, float, bool, type(None), bytes)


class Table:
    def __init__(self):
        self.classes: list = []  # (module, qualname)
        self.class_ix: dict = {}
        self.nodes: list = []  # node records
        self.ix: dict = {}  # record pickle -> node id
        self.sigs: list = []
        self.sig_ix: dict = {}
        self.refs = 0  # node references made (incl. dedup hits)

    def cls(self, c):
        i = self.class_ix.get(c)
        if i is None:
            i = self.class_ix[c] = len(self.classes)
            self.classes.append((c.__module__, c.__qualname__))
        return i

    def add(self, rec) -> int:
        self.refs += 1
        key = pickle.dumps(rec, protocol=5)
        i = self.ix.get(key)
        if i is None:
            i = self.ix[key] = len(self.nodes)
            self.nodes.append(rec)
        return i

    def sig(self, s) -> int:
        i = self.sig_ix.get(s)
        if i is None:
            i = self.sig_ix[s] = len(self.sigs)
            self.sigs.append(s)
        return i


class Enc:
    """Encoder state for ONE call input."""

    def __init__(self, tab, base):
        self.tab, self.base = tab, base
        self.roots = {}  # id(root) -> (k, root node id, root)
        self.types = {}  # id(parentless type object) -> ordinal
        self.cur = None  # (k, root) being encoded

    @staticmethod
    def root_of(x):
        hops = 0
        while x.parent is not None:
            x = x.parent
            hops += 1
            if hops > 1_000_000:
                raise NotCanonical("cycle")
        return x

    @staticmethod
    def path(root, obj):
        steps = []
        x = obj
        while x is not root:
            steps.append((x.arg_key, x.index))
            x = x.parent
        steps.reverse()
        p = tuple(steps)
        if _walk(root, p) is not obj:
            raise NotCanonical("path")
        return p

    def ensure_root(self, root):
        hit = self.roots.get(id(root))
        if hit is not None:
            return hit, None
        k = len(self.roots)
        self.cur = (k, root)
        sig: list = []
        rid = self.node(root, set(), sig)
        self.cur = None
        sid = self.tab.sig(tuple(sig))
        hit = self.roots[id(root)] = (k, rid, root)
        return hit, sid

    def type_entry(self, ty):
        if not isinstance(ty, self.base):
            raise NotCanonical("type not Expr")
        if ty.parent is None and ty is not self.cur[1] and id(ty) not in self.roots:
            o = self.types.get(id(ty))
            if o is not None:
                return ("old", o)
            self.types[id(ty)] = len(self.types)
            return ("new", self.node(ty, set(), None))
        if self.root_of(ty) is not self.cur[1]:
            raise NotCanonical("type is a node of another tree")
        return ("node", self.cur[0], self.path(self.cur[1], ty))

    def val(self, v, owner, key, index, seen, sig):
        t = type(v)
        if isinstance(v, self.base):
            if id(v) in seen:
                raise NotCanonical("alias node")
            if v.parent is not owner or v.arg_key != key or v.index != index:
                raise NotCanonical("parent link")
            return ("n", self.node(v, seen, sig))
        if t is list:
            if id(v) in seen:
                raise NotCanonical("alias list")
            seen.add(id(v))
            return ("l", tuple(self.val(x, owner, key, i, seen, sig) for i, x in enumerate(v)))
        if t in _LIT:
            return v
        if isinstance(v, enum.Enum):
            return ("e", self.tab.cls(t), v.name)
        raise NotCanonical(f"arg type {t.__name__}")

    def plain(self, v, seen):
        t = type(v)
        if t in _LIT:
            return v
        if t in (list, dict):
            if id(v) in seen:
                raise NotCanonical("alias plain")
            seen.add(id(v))
            if t is list:
                return ("L", tuple(self.plain(x, seen) for x in v))
            return ("D", tuple((self.plain(k, seen), self.plain(x, seen)) for k, x in v.items()))
        if isinstance(v, enum.Enum):
            return ("e", self.tab.cls(t), v.name)
        raise NotCanonical(f"plain {t.__name__}")

    def node(self, n, seen, sig) -> int:
        seen.add(id(n))
        if getattr(n, "__dict__", None):
            raise NotCanonical("instance dict")
        args = n.args
        if type(args) is not dict or id(args) in seen:
            raise NotCanonical("args")
        seen.add(id(args))
        a = tuple((k, self.val(v, n, k, None, seen, sig)) for k, v in args.items())
        ty = n._type
        if ty is not None:
            if sig is None:
                raise NotCanonical("typed node inside a type")
            sig.append(self.type_entry(ty))
        rec = (self.tab.cls(type(n)), a,
               None if n.comments is None else self.plain(n.comments, seen),
               ty is not None,
               None if n._meta is None else self.plain(n._meta, seen),
               n._hash)
        return self.tab.add(rec)


class HPickler(serial.InPickler):
    def __init__(self, f, enc):
        super().__init__(f, protocol=5)
        self.enc = enc

    def persistent_id(self, obj):
        e = self.enc
        if not isinstance(obj, e.base):
            return None
        root = e.root_of(obj)
        (k, rid, _), sid = e.ensure_root(root)
        p = e.path(root, obj)
        if sid is None:
            return ("E", k, p)
        return ("R", rid, k, p, sid)


def encode(tab, value, base) -> bytes:
    buf = io.BytesIO()
    HPickler(buf, Enc(tab, base)).dump(value)
    return buf.getvalue()


# ---------------------------------------------------------------- decoding
def _walk(root, path):
    x = root
    for key, i in path:
        v = x.args[key]
        x = v[i] if i is not None else v
    return x


class Decoder:
    def __init__(self, tab):
        self.tab = tab
        self.classes = []
        self.refresh()

    def refresh(self):
        for mod, qn in self.tab.classes[len(self.classes):]:
            o = importlib.import_module(mod)
            for part in qn.split("."):
                o = getattr(o, part)
            self.classes.append(o)

    def _plain(self, v):
        if type(v) is tuple:
            if v[0] == "L":
                return [self._plain(x) for x in v[1]]
            if v[0] == "D":
                return {self._plain(k): self._plain(x) for k, x in v[1]}
            return getattr(self.classes[v[1]], v[2])
        return v

    def build(self, nid, typed, parent=None, key=None, index=None):
        cls_i, a, com, has_type, meta, h = self.tab.nodes[nid]
        cls = self.classes[cls_i]
        n = cls.__new__(cls)
        args = {}
        for k, v in a:
            if type(v) is tuple:
                tag = v[0]
                if tag == "n":
                    v = self.build(v[1], typed, n, k, None)
                elif tag == "e":
                    v = getattr(self.classes[v[1]], v[2])
                else:
                    out = []
                    for i, x in enumerate(v[1]):
                        if type(x) is tuple:
                            x = (self.build(x[1], typed, n, k, i) if x[0] == "n"
                                 else getattr(self.classes[x[1]], x[2]))
                        out.append(x)
                    v = out
            args[k] = v
        n.args = args
        n.parent = parent
        n.arg_key = key
        n.index = index
        n.comments = None if com is None else self._plain(com)
        n._type = None
        if has_type:
            typed.append(n)
        n._meta = None if meta is None else self._plain(meta)
        n._hash = h
        return n


class HUnpickler(serial._InUnpickler):
    def __init__(self, f, dec):
        super().__init__(f)
        self.dec = dec
        self.trees = {}  # k -> root
        self.todo = []  # (typed nodes, sig) per root, in first-reference order

    def persistent_load(self, pid):
        tag = pid[0]
        if tag == "E":
            return _walk(self.trees[pid[1]], pid[2])
        if tag == "R":
            _, rid, k, path, sid = pid
            typed: list = []
            root = self.trees[k] = self.dec.build(rid, typed)
            self.todo.append((typed, self.dec.tab.sigs[sid]))
            return _walk(root, path)
        return super().persistent_load(pid)

    def finish(self):
        types: list = []
        for typed, sig in self.todo:
            assert len(typed) == len(sig)
            for n, ent in zip(typed, sig):
                kind = ent[0]
                if kind == "new":
                    t = self.dec.build(ent[1], None)
                    types.append(t)
                elif kind == "old":
                    t = types[ent[1]]
                else:
                    t = _walk(self.trees[ent[1]], ent[2])
                n._type = t


def decode(dec, data: bytes):
    u = HUnpickler(io.BytesIO(data), dec)
    v = u.load()
    u.finish()
    return v
