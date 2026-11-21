"""pytest plugin: per test, record modules and module:Class of every Dialect/Parser/Generator/Tokenizer/Expression class (full MRO) instantiated.
Run: pytest -p usemap_plugin (with this dir on PYTHONPATH); writes $USEMAP_OUT.<pid>.json"""
import json, os, collections
_cur = [None]; _use = collections.defaultdict(set)
def _wrap(cls):
    orig = cls.__init__; own = "__init__" in cls.__dict__
    def __init__(self, *a, **k):
        if _cur[0]:
            for c in type(self).__mro__:
                m = getattr(c, "__module__", "")
                if m.startswith("sqlglot"):
                    _use[_cur[0]].add(m); _use[_cur[0]].add(m + ":" + c.__qualname__)
        # if cls only inherits __init__, defer along the instance MRO (e.g. Week -> Expression -> ... -> TimeUnit.__init__)
        return orig(self, *a, **k) if own else super(cls, self).__init__(*a, **k)
    cls.__init__ = __init__
def pytest_configure(config):
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.generator import Generator
    from sqlglot.tokens import Tokenizer
    from sqlglot.expressions.core import Expression
    for c in (Dialect, Parser, Generator, Tokenizer, Expression): _wrap(c)
def pytest_runtest_setup(item): _cur[0] = item.nodeid
def pytest_runtest_teardown(item): _cur[0] = None
def pytest_unconfigure(config):
    json.dump({k: sorted(v) for k, v in _use.items()}, open(f"{os.environ['USEMAP_OUT']}.{os.getpid()}.json", "w"))
