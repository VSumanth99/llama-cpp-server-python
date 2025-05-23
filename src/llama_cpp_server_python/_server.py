from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
import requests
from llama_cpp_server_python._binary import download_binary
from llama_cpp_server_python._model import download_model

if TYPE_CHECKING:
    import io


class Server:
    """
    Wrapper around the llama-server binary.

    Examples
    --------
    >>> from openai import OpenAI
    >>> from llama_cpp_server_python import Server
    >>> repo = "Qwen/Qwen2-0.5B-Instruct-GGUF"
    >>> filename = "qwen2-0_5b-instruct-q4_0.gguf"
    >>> with Server.from_huggingface(repo=repo, filename=filename) as server:
    ...     client = OpenAI(base_url=server.base_url)
    ...     pass  # interact with the client

    For more control over the server, you can download the model and binary
    separately, and pass in other parameters:

    >>> binary_path = "path/to/llama-server"
    >>> model_path = "path/to/model.gguf"
    >>> server = Server(binary_path=binary_path, model_path=model_path, port=6000, ctx_size=1024)
    >>> server.start()
    >>> server.wait_for_ready()
    >>> client = OpenAI(base_url=server.base_url)
    >>> pass  # interact with the client
    >>> server.stop() # or use a context manager as above
    """

    def __init__(
        self,
        *,
        binary_path: str | Path,
        model_path: str | Path,
        port: int = 8080,
        n_gpu_layers: int = 0,
        ctx_size: int = 512,
        parallel: int = 8,
        cont_batching: bool = True,
        logger: logging.Logger | None = None,
    ) -> None:
        """
        Create a server instance (but don't start it yet).

        For details on most parameters see
        https://github.com/ggerganov/llama.cpp/tree/master/examples/server

        Parameters
        ----------
        binary_path :
            The path to the llama-server binary.
        model_path :
            The path to the model weights file.
            Must be a .gguf file, per the llama.cpp model format.
        port :
            The port to run the server on.
        n_gpu_layers :
            The number of GPU layers to offload the model to.
        ctx_size :
            The context size for each request.
            Note this is a different meaning from how the raw binary interprets it:
            the raw binary uses this as the total context size, spread across all
            parallel requests.
        parallel :
            The number of parallel requests to handle.
        cont_batching :
            Whether to use continuous batching.
        logger :
            A logger to use for logging server output.
            If None, a new logger is created.
            You can configure the logger with handlers, formatters, etc. after
            creating the server as needed.
        """
        self.binary_path = Path(binary_path)
        self.model_path = Path(model_path)
        self.port = port
        self.ctx_size = ctx_size
        self.parallel = parallel
        self.cont_batching = cont_batching

        if n_gpu_layers == -1:
            self.ngl = 0x7fffffff # max value of int so all layers are offloaded
        else:
            assert n_gpu_layers >= 0, "n_gpu_layers must be non-negative."
            self.ngl = n_gpu_layers
        
        self._check_resources()

        if logger is None:
            logger = logging.getLogger(__name__ + ".Server" + str(self.port))
        self._logger = logger
        self._process = None

    @classmethod
    def from_huggingface(
        cls, *, repo: str, filename: str, working_dir: str | Path = "./llama"
    ) -> "Server":
        """Create a server from a HuggingFace model repository.

        If you need more control, download the model and binary separately,
        and then call the constructor directly.

        Parameters
        ----------
        repo :
            The HuggingFace model repository, eg "Qwen/Qwen2-0.5B-Instruct-GGUF".
        filename :
            The filename of the model weights, eg "qwen2-0_5b-instruct-q4_0.gguf".
        working_dir :
            The working directory to download the model and server binary to.

        Returns
        -------
        Server
        """
        working_dir = Path(working_dir)
        binary_path = working_dir / "llama-server"
        model_path = working_dir / filename
        if not binary_path.exists():
            download_binary(binary_path)
        if not model_path.exists():
            download_model(dest=model_path, repo=repo, filename=filename)
        return cls(binary_path=binary_path, model_path=model_path)

    @property
    def base_url(self) -> str:
        """The base URL of the server, e.g. 'http://127.0.0.1:8080'."""
        return f"http://127.0.0.1:{self.port}"

    @property
    def logger(self) -> logging.Logger:
        """The logger used for server output."""
        return self._logger

    @logger.setter
    def logger(self, logger: logging.Logger):
        self._logger = logger
        if self._process is not None:
            self._process.logger = logger

    def start(self, wait=True, timeout=180) -> None:
        """Start the server in a subprocess.

        This returns immediately `if wait=False`.

        Pair this with a .stop() call when you are done.
        Or, use a context manager with 'with Server(...) as server: ...'
        to automatically start and stop the server.

        You can start and stop the server multiple times in a row.
        """
        if self._process is not None:
            raise RuntimeError("Server is already running.")
        self._check_resources()
        print(
            f"Starting server with command: '{' '.join(self._command)}'..."
        )
        self._process = _RunningServerProcess(self._command, self.logger)
        if wait:
            self.wait_for_ready(timeout=timeout)

    def stop(self) -> None:
        """Terminate the server subprocess. No-op if there is no active subprocess."""
        if self._process is None:
            return
        self._process.stop()
        self._process = None

    def wait_for_ready(self, timeout: int = 180) -> None:
        """
        Wait until llama-server is accepting TCP connections on self.port.
        This is more reliable than grepping its logs.
        """
        if self._process is None:
            raise RuntimeError("Server is not running.")

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = requests.get(self.base_url, timeout=1.0)
            except requests.ConnectionError:
                # server not yet up at all
                pass
            else:
                if r.status_code == 200:
                    return
                if r.status_code not in (503,):
                    r.raise_for_status()
            time.sleep(0.5)

        raise TimeoutError(
            f"Server did not start listening on port {self.port} "
            f"within {timeout} seconds."
        )


    def __enter__(self):
        """Start the server when entering a context manager."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop the server when exiting a context manager."""
        self.stop()

    @property
    def _command(self) -> list[str]:
        cmd = [str(self.binary_path)]
        cmd.extend(["--model", str(self.model_path)])
        # cmd.extend(["--host", "127.0.0.1"])
        cmd.extend(["--port", f"{self.port}"])
        cmd.extend(["--ctx_size", f"{self.ctx_size * self.parallel}"])
        cmd.extend(["--parallel", f"{self.parallel}"])
        cmd.extend(["-ngl", f"{self.ngl}"])
        cmd.extend(["--split-mode", "row"])
        if self.cont_batching:
            cmd.append("--cont_batching")
        return cmd

    def _check_resources(self) -> None:
        if not self.binary_path.exists():
            raise FileNotFoundError(f"Server binary not found at {self.binary_path}.")
        if not self.model_path.exists():
            raise FileNotFoundError(f"Model weights not found at {self.model_path}.")


class _RunningServerProcess:
    def __init__(self, args: list[str], logger: logging.Logger) -> None:
        self.popen = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.logger = logger
        self._logging_threads = self._watch_outputs()
        self._status = "starting"

    def wait_for_ready(self, *, timeout: int = 5) -> None:
        if self._status == "running":
            return
        start = time.time()
        while time.time() - start < timeout:
            self._check_not_exited()
            if self._status == "running":
                print("Server started.")
                return
            time.sleep(0.1)
        raise TimeoutError(f"Server did not start within {timeout} seconds.")

    def _check_not_exited(self) -> None:
        exit_code = self.popen.poll()
        if exit_code is None:
            return
        self.stop()
        raise RuntimeError(
            f"Server exited unexpectedly with code {self.popen.returncode}."
        )

    def _watch_outputs(self) -> list[threading.Thread]:
        def watch(file: io.StringIO):
            for line in file:
                line = line.strip()
                if "HTTP server listening" in line:
                    self._status = "running"
                print(line)

        std_out_thread = threading.Thread(target=watch, args=(self.popen.stdout,))
        std_err_thread = threading.Thread(target=watch, args=(self.popen.stderr,))
        std_out_thread.start()
        std_err_thread.start()
        return [std_out_thread, std_err_thread]

    def stop(self):
        self.popen.kill()
        for thread in self._logging_threads:
            thread.join()
