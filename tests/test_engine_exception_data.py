"""Only detached plain exception data may leave the COM apartment."""
import pytest

from agent.audio.engine import _detach_owner_exception


@pytest.mark.parametrize("error", [
    ValueError("plain message"),
    OSError(2, "missing file", "example.wav"),
    StopIteration(42),
    UnicodeDecodeError("utf8", b"\xff", 0, 1, "invalid byte"),
    SyntaxError("invalid source", ("source.py", 3, 4, "broken source")),
])
def test_builtin_exception_data_preserves_type_message_and_ordinary_fields(error):
    detached = _detach_owner_exception(error)
    assert detached is not error and type(detached) is type(error)
    assert detached.args == error.args
    assert str(detached) == str(error)
    assert detached.__traceback__ is None
    assert detached.__cause__ is None and detached.__context__ is None
    for name in ("filename", "errno", "value", "encoding", "object", "start", "end", "reason", "lineno", "offset"):
        if hasattr(error, name):
            assert getattr(detached, name) == getattr(error, name)


def test_native_attribute_and_cyclic_args_use_text_only_fallback():
    error = RuntimeError("native attribute")
    error.interface = object()
    detached = _detach_owner_exception(error)
    assert type(detached) is RuntimeError and str(detached) == "native attribute"
    assert detached.original_type == "builtins.RuntimeError"
    assert not hasattr(detached, "interface")
    cyclic = []
    cyclic.append(cyclic)
    error = ValueError(cyclic)
    detached = _detach_owner_exception(error)
    assert type(detached) is RuntimeError and str(detached) == str(error)
    assert detached.original_type == "builtins.ValueError"


def test_com_error_primitive_details_remain_supported_when_runtime_available():
    try:
        from _ctypes import COMError
    except ImportError:
        pytest.skip("COMError requires Windows")
    error = COMError(-2147467259, "native failure", ("description", "source", "helpfile", 0, None))
    detached = _detach_owner_exception(error)
    assert type(detached) is COMError
    assert detached.args == error.args and str(detached) == str(error)
    assert detached.hresult == error.hresult and detached.details == error.details
