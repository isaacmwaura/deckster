"""P6: autostart registry round-trip and bundled-resource path resolution."""
from agent import autostart
from agent.config import resource_root


def test_resource_root_finds_web_shell():
    index = resource_root() / "web" / "index.html"
    assert index.exists(), "web/index.html must resolve via resource_root()"


def test_autostart_command_is_nonempty():
    cmd = autostart._command()
    assert cmd and ("agent.main" in cmd or cmd.endswith('"'))


def test_autostart_enable_disable_roundtrip():
    name = "StreamControlTest_pytest"
    try:
        assert autostart.is_enabled(name) is False
        assert autostart.enable(name, command='"C:\\dummy\\StreamControl.exe"') is True
        assert autostart.is_enabled(name) is True
        assert autostart.disable(name) is True
        assert autostart.is_enabled(name) is False
    finally:
        autostart.disable(name)  # ensure cleanup even on assertion failure


def test_autostart_disable_absent_is_ok():
    assert autostart.disable("StreamControlTest_does_not_exist") is True
