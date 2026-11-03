# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
import time
from unittest.mock import MagicMock, patch

import jupyter_kernel_client
import pytest

from colab_cli.runtime import ColabRuntime


def test_colab_runtime_kernel_client():
    if hasattr(jupyter_kernel_client, "ColabKernelClient"):
        target_attr = "ColabKernelClient"
        token_param_name = "proxy_token"
    elif hasattr(jupyter_kernel_client, "KernelClient"):
        target_attr = "KernelClient"
        token_param_name = "token"
    else:
        target_attr = "JupyterKernelClient"
        token_param_name = "token"

    with patch.object(jupyter_kernel_client, target_attr) as mock_kc_cls:
        mock_kc = mock_kc_cls.return_value
        runtime = ColabRuntime("http://url", "token123")

        assert runtime._kernel_client is None

        kc = runtime.kernel_client

        expected_kwargs = {
            "server_url": "http://url",
            token_param_name: "token123",
            "kernel_id": None,
            "client_kwargs": {
                "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
                "extra_params": {"colab-runtime-proxy-token": "token123"},
            },
            "headers": {
                "X-Colab-Client-Agent": "colab-cli",
                "X-Colab-Runtime-Proxy-Token": "token123",
            },
        }
        mock_kc_cls.assert_called_once_with(**expected_kwargs)
        mock_kc.start.assert_called_once()
        assert kc == mock_kc


def test_colab_runtime_kernel_client_jupyterclient_fallback():
    """PyPI jupyter-kernel-client>=1.0 renamed KernelClient to
    JupyterKernelClient. Runtime must fall back to it instead of raising
    AttributeError (breaks `colab exec` on `uv tool install google-colab-cli`)."""
    import colab_cli.runtime as runtime_mod

    real_colab = getattr(jupyter_kernel_client, "ColabKernelClient", None)
    real_legacy = getattr(jupyter_kernel_client, "KernelClient", None)
    had_colab = hasattr(jupyter_kernel_client, "ColabKernelClient")
    had_legacy = hasattr(jupyter_kernel_client, "KernelClient")

    if had_colab:
        delattr(jupyter_kernel_client, "ColabKernelClient")
    if had_legacy:
        delattr(jupyter_kernel_client, "KernelClient")
    try:
        with patch.object(
            jupyter_kernel_client, "JupyterKernelClient", create=True
        ) as mock_kc_cls:
            mock_kc = mock_kc_cls.return_value
            runtime = ColabRuntime("http://url", "token123")
            kc = runtime.kernel_client
            mock_kc_cls.assert_called_once_with(
                server_url="http://url",
                token="token123",
                kernel_id=None,
                client_kwargs={
                    "subprotocol": jupyter_kernel_client.JupyterSubprotocol.DEFAULT,
                    "extra_params": {"colab-runtime-proxy-token": "token123"},
                },
                headers={
                    "X-Colab-Client-Agent": "colab-cli",
                    "X-Colab-Runtime-Proxy-Token": "token123",
                },
            )
            mock_kc.start.assert_called_once()
            assert kc == mock_kc
    finally:
        if had_colab:
            jupyter_kernel_client.ColabKernelClient = real_colab
        if had_legacy:
            jupyter_kernel_client.KernelClient = real_legacy
        assert runtime_mod  # keep import used for clarity


def test_colab_runtime_execute_code():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    # Test empty reply
    mock_kc.execute.return_value = {}
    assert runtime.execute_code("print(1)") == []

    # Test normal reply
    mock_kc.execute.return_value = {"outputs": [{"text": "1\n"}]}
    assert runtime.execute_code("print(1)") == [{"text": "1\n"}]

    # Test error status without error output
    mock_kc.execute.return_value = {
        "status": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "outputs": [{"text": "partial"}],
    }
    outputs = runtime.execute_code("raise ValueError")
    assert len(outputs) == 2
    assert outputs[0] == {"text": "partial"}
    assert outputs[1] == {
        "output_type": "error",
        "ename": "ValueError",
        "evalue": "bad",
        "traceback": [],
    }


@pytest.mark.parametrize("timeout", [None, 600])
@pytest.mark.parametrize("interactive", [False, True], ids=["execute", "execute_interactive"])
def test_kernel_client_never_gets_a_deadline(timeout, interactive):
    """jupyter_kernel_client treats `timeout` as a fixed deadline: past it,
    the output loop busy-waits and the final reply is awaited for 0 s, so long
    runs failed with "Timeout waiting for reply" (seen live on 2026-10-08).
    The runtime always passes timeout=None and enforces limits itself."""
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    mock_kc.execute.return_value = {"outputs": []}
    mock_kc.execute_interactive.return_value = {"content": {"status": "ok"}}

    if interactive:
        runtime.execute_code("print(1)", output_hook=lambda o: None, timeout=timeout)
        _, kwargs = mock_kc.execute_interactive.call_args
    else:
        runtime.execute_code("print(1)", timeout=timeout)
        _, kwargs = mock_kc.execute.call_args
    assert kwargs["timeout"] is None
    mock_kc.interrupt.assert_not_called()


def test_timeout_interrupts_the_kernel_and_raises():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc
    released = threading.Event()

    def slow_execute(code, output_hook=None, **kwargs):
        # The kernel finishes (with KeyboardInterrupt) once it is interrupted.
        released.wait(5)
        return {"content": {"status": "error", "ename": "KeyboardInterrupt"}}

    mock_kc.execute_interactive.side_effect = slow_execute
    mock_kc.interrupt.side_effect = lambda *a, **k: released.set()

    started = time.monotonic()
    with pytest.raises(TimeoutError, match="0.2 s timeout; the kernel was interrupted"):
        runtime.execute_code("while True: pass", output_hook=lambda o: None, timeout=0.2)
    assert time.monotonic() - started < 3
    mock_kc.interrupt.assert_called_once()


def test_colab_runtime_stop():
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    runtime.stop()
    mock_kc._manager.client.stop_channels.assert_called_once()


def test_colab_runtime_stop_exception(caplog):
    runtime = ColabRuntime("http://url", "token123")
    mock_kc = MagicMock()
    mock_kc._manager.client.stop_channels.side_effect = Exception("Stop failed")
    runtime._kernel_client = mock_kc

    runtime.stop()  # Should not raise
    assert "Error stopping kernel client" in caplog.text


def test_colab_runtime_stdin_logging():
    mock_history = MagicMock()
    runtime = ColabRuntime(
        "http://url", "token", session_name="test-s", history=mock_history
    )
    mock_kc = MagicMock()
    runtime._kernel_client = mock_kc

    mock_kc.execute.side_effect = lambda code, allow_stdin=False, stdin_hook=None, **kw: {
        "outputs": [{"text": stdin_hook("Enter something: ")}]
    }

    with patch("colab_cli.runtime.input", return_value="user input"):
        outputs = runtime.execute_code("code", allow_stdin=True)

    assert outputs == [{"text": "user input"}]
    mock_history.log_event.assert_any_call(
        "test-s", "stdin_request", {"prompt": "Enter something: "}
    )
    mock_history.log_event.assert_any_call(
        "test-s", "input_reply", {"value": "user input"}
    )


def _runtime_with_kernel(kernel):
    runtime = ColabRuntime("http://url", "token123", session_name="s1")
    runtime._kernel_client = kernel
    return runtime


def test_execute_code_names_a_dropped_connection():
    """jupyter_kernel_client raises a bare RuntimeError when the kernel
    websocket drops (e.g. `colab stop` mid-run); surface it as
    SessionConnectionLost so the CLI can explain it instead of a traceback."""
    import pytest

    from colab_cli.runtime import SessionConnectionLost

    kernel = MagicMock()
    kernel.execute_interactive.side_effect = RuntimeError("Connection was lost.")
    kernel.execute.side_effect = RuntimeError("Connection was lost.")
    runtime = _runtime_with_kernel(kernel)

    with pytest.raises(SessionConnectionLost) as exc:
        runtime.execute_code("1", output_hook=lambda o: None)
    assert exc.value.session_name == "s1"
    with pytest.raises(SessionConnectionLost):
        runtime.execute_code("1")


def test_execute_code_keeps_other_runtime_errors():
    import pytest

    from colab_cli.runtime import SessionConnectionLost

    kernel = MagicMock()
    kernel.execute.side_effect = RuntimeError("something else")
    runtime = _runtime_with_kernel(kernel)

    with pytest.raises(RuntimeError, match="something else") as exc:
        runtime.execute_code("1")
    assert not isinstance(exc.value, SessionConnectionLost)
