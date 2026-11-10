import builtins
import importlib
import sys

import pytest


def test_local_import_without_the_extra_names_the_extra(monkeypatch):
    for name in [n for n in sys.modules if n == "cosens.local" or n.startswith("cosens.local.") or n.split(".")[0] == "jsonschema"]:
        monkeypatch.delitem(sys.modules, name)
    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.split(".")[0] == "jsonschema":
            raise ModuleNotFoundError("No module named 'jsonschema'", name="jsonschema")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ImportError, match=r"cosens-sdk\[local\]"):
        importlib.import_module("cosens.local")
