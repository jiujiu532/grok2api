import base64
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch


def _load_headers_module():
    logger_stub = types.SimpleNamespace(debug=lambda *args, **kwargs: None)
    stubs = {
        "app": types.ModuleType("app"),
        "app.platform": types.ModuleType("app.platform"),
        "app.platform.logging": types.ModuleType("app.platform.logging"),
        "app.platform.logging.logger": types.SimpleNamespace(logger=logger_stub),
        "app.platform.config": types.ModuleType("app.platform.config"),
        "app.platform.config.snapshot": types.SimpleNamespace(get_config=lambda: None),
        "app.control": types.ModuleType("app.control"),
        "app.control.proxy": types.ModuleType("app.control.proxy"),
        "app.control.proxy.models": types.SimpleNamespace(ProxyLease=object),
        "app.dataplane": types.ModuleType("app.dataplane"),
        "app.dataplane.proxy": types.ModuleType("app.dataplane.proxy"),
        "app.dataplane.proxy.adapters": types.ModuleType(
            "app.dataplane.proxy.adapters"
        ),
        "app.dataplane.proxy.adapters.profile": types.SimpleNamespace(
            ProxyProfile=object,
            resolve_proxy_profile=lambda lease: None,
        ),
    }
    file_path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "app/dataplane/proxy/adapters/headers.py"
    )
    spec = importlib.util.spec_from_file_location("test_headers_module", file_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    return module


headers = _load_headers_module()


class _DummyConfig:
    def get_bool(self, key, default=False):
        if key == "features.dynamic_statsig":
            return True
        return default


class StatsigIdTests(unittest.TestCase):
    def test_dynamic_statsig_uses_x1_prefix(self):
        with patch.object(headers, "get_config", return_value=_DummyConfig()):
            with patch.object(headers.random, "choice", return_value=True):
                value = headers._statsig_id()

        decoded = base64.b64decode(value).decode()
        self.assertTrue(decoded.startswith("x1:TypeError:"))


if __name__ == "__main__":
    unittest.main()
