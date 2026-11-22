"""r29: the stage guard of selector/subtest.py (no checkpoint shortcut for state writers) and
the type-strict tree digest."""

import os
import tempfile
import textwrap
import unittest

from selector.subtest import stage_guard

BASE = textwrap.dedent('''\
    import sqlglot.executor.env as envmod
    from sqlglot import exp

    TABLE = {}


    def f(expression, n):
        x = n + 1
        return x


    class Dialect:
        def g(self, e):
            return e


    class Simplifier:
        def h(self, e):
            return e
''')


def guard(path: str, old: str, new: str):
    with tempfile.TemporaryDirectory() as d:
        full = os.path.join(d, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(old)
        return stage_guard(d, [(path, new, set())])


def edit(body_old: str, body_new: str) -> str:
    assert body_old in BASE
    return BASE.replace(body_old, body_new)


class StageGuard(unittest.TestCase):
    P = "sqlglot/optimizer/x.py"

    def test_local_change_ok(self):
        self.assertIsNone(guard(self.P, BASE, edit("x = n + 1", "x = n - 1")))

    def test_param_mutation_ok(self):
        # the query's tree / scopes are passed in and compared by the digest
        new = edit("x = n + 1", "expression.set('this', None)\n    x = n + 1")
        self.assertIsNone(guard(self.P, BASE, new))

    def test_fresh_local_ok(self):
        new = edit("x = n + 1", "d = {}\n    d['a'] = 1\n    x = exp.column('a')\n    x.set('b', 1)")
        self.assertIsNone(guard(self.P, BASE, new))

    def test_global_statement(self):
        new = edit("x = n + 1", "global TABLE\n    TABLE = {}\n    x = n")
        self.assertIn("global", guard(self.P, BASE, new))

    def test_module_dict_store(self):
        new = edit("x = n + 1", "TABLE['a'] = n\n    x = n")
        self.assertIn("TABLE", guard(self.P, BASE, new))

    def test_import_alias_store(self):
        new = edit("x = n + 1", "from sqlglot.executor.env import ENV\n    ENV['SUM'] = n\n    x = n")
        self.assertIn("ENV", guard(self.P, BASE, new))

    def test_alias_chain(self):
        new = edit("x = n + 1", "t = envmod.ENV\n    t['SUM'] = n\n    x = n")
        self.assertIn("t", guard(self.P, BASE, new))

    def test_mutator_method_on_module(self):
        new = edit("x = n + 1", "envmod.ENV.update({})\n    x = n")
        self.assertIn("update", guard(self.P, BASE, new))

    def test_setattr(self):
        new = edit("x = n + 1", "setattr(exp.Sum, 'a', 1)\n    x = n")
        self.assertIn("setattr", guard(self.P, BASE, new))

    def test_class_attr_through_exp(self):
        new = edit("x = n + 1", "exp.Sum.is_var_len_args = True\n    x = n")
        self.assertIn("exp", guard(self.P, BASE, new))

    def test_dialect_self_store(self):
        new = edit("        return e\n\n\nclass Simplifier",
                   "        self.cache = e\n        return e\n\n\nclass Simplifier")
        self.assertIsNotNone(guard(self.P, BASE, new))

    def test_per_call_self_store_ok(self):
        new = edit("class Simplifier:\n    def h(self, e):\n        return e",
                   "class Simplifier:\n    def h(self, e):\n        self.seen = e\n        return e")
        self.assertIsNone(guard(self.P, BASE, new))

    def test_old_version_checked_too(self):
        old = edit("x = n + 1", "TABLE['a'] = n\n    x = n")
        new = edit("x = n + 1", "TABLE['a'] = n\n    x = n + 2")
        self.assertIsNotNone(guard(self.P, old, new))

    def test_loop_over_global(self):
        new = edit("x = n + 1", "for v in TABLE.values():\n        v.append(1)\n    x = n")
        self.assertIsNotNone(guard(self.P, BASE, new))


class StrictDigest(unittest.TestCase):
    def test_type_and_meta_matter(self):
        try:
            from sqlglot import exp, parse_one
        except ImportError:
            self.skipTest("sqlglot not importable")
        from selector.subtest import strict_digest
        a = parse_one("SELECT a FROM t")
        b = parse_one("SELECT a FROM t")
        self.assertEqual(strict_digest(a), strict_digest(b))
        b.find(exp.Column).type = "int"
        self.assertNotEqual(strict_digest(a), strict_digest(b))
        c = parse_one("SELECT a FROM t")
        c.find(exp.Column).meta["x"] = 1
        self.assertNotEqual(strict_digest(a), strict_digest(c))


if __name__ == "__main__":
    unittest.main()
