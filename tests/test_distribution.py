from __future__ import annotations

import json
import os
import subprocess
import tarfile
import threading
import venv
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import pytest

from mica_agent.jsonl_store import JsonlSessionStore
from mica_agent.session_service import SessionService
from mica_agent.workspace import resolve_workspace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.1.0"
PACKAGE_ROOT = f"mica_agent-{VERSION}"


@dataclass(frozen=True)
class BuiltDistribution:
    wheel: Path
    sdist: Path


@dataclass(frozen=True)
class InstalledDistribution:
    python: Path
    mica: Path
    outside_repository: Path
    home: Path


EntryFactory = Callable[[InstalledDistribution], list[str | Path]]


class LoopbackSSEServer:
    """为已安装 CLI 提供确定性 OpenAI-compatible SSE 响应。"""

    def __init__(self, response_texts: tuple[str, ...]) -> None:
        self._responses = tuple(self._make_response(text) for text in response_texts)
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                try:
                    length = int(self.headers["Content-Length"] or "0")
                    payload = json.loads(self.rfile.read(length))
                except (TypeError, ValueError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                if not isinstance(payload, dict):
                    self.send_error(400)
                    return
                with owner._lock:
                    response_index = len(owner.requests)
                    owner.requests.append(payload)
                if response_index >= len(owner._responses):
                    self.send_error(500)
                    return
                body = owner._responses[response_index]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mica-p3-06-loopback",
            daemon=True,
        )

    @staticmethod
    def _make_response(text: str) -> bytes:
        def chunk(delta: dict[str, object], finish_reason: str | None) -> str:
            return (
                "data: "
                + json.dumps(
                    {
                        "choices": [
                            {
                                "index": 0,
                                "delta": delta,
                                "finish_reason": finish_reason,
                            }
                        ]
                    },
                    separators=(",", ":"),
                )
                + "\n\n"
            )

        return (
            chunk({"role": "assistant"}, None)
            + chunk({"content": text}, None)
            + chunk({}, "stop")
            + "data: [DONE]\n\n"
        ).encode("utf-8")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("loopback SSE server did not stop")


class LoopbackToolSSEServer:
    """为安装态 CLI 提供一次 write 工具调用和后续最终回答。"""

    def __init__(
        self,
        *,
        tool_name: str = "write",
        arguments: dict[str, object] | None = None,
        response_texts: tuple[str, ...] = ("first tool answer", "continued answer"),
    ) -> None:
        self._tool_name = tool_name
        self._arguments = arguments or {
            "path": "目标文件.txt",
            "content": "sk-p5-08-sensitive-sentinel",
        }
        self._response_texts = response_texts
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"] or "0")
                payload = json.loads(self.rfile.read(length))
                with owner._lock:
                    index = len(owner.requests)
                    owner.requests.append(payload)
                if index == 0:
                    body = owner._tool_response()
                elif index - 1 < len(owner._response_texts):
                    body = owner._text_response(owner._response_texts[index - 1])
                else:
                    self.send_error(500)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="mica-p5-08-loopback",
            daemon=True,
        )

    @staticmethod
    def _chunk(delta: dict[str, object], finish_reason: str | None) -> str:
        return (
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "delta": delta,
                            "finish_reason": finish_reason,
                        }
                    ]
                },
                separators=(",", ":"),
            )
            + "\n\n"
        )

    def _tool_response(self) -> bytes:
        return (
            self._chunk({"role": "assistant"}, None)
            + self._chunk(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "installed-write-call",
                            "type": "function",
                            "function": {
                                "name": self._tool_name,
                                "arguments": json.dumps(
                                    self._arguments,
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            },
                        }
                    ]
                },
                None,
            )
            + self._chunk({}, "tool_calls")
            + "data: [DONE]\n\n"
        ).encode("utf-8")

    @classmethod
    def _text_response(cls, text: str) -> bytes:
        return (
            cls._chunk({"role": "assistant"}, None)
            + cls._chunk({"content": text}, None)
            + cls._chunk({}, "stop")
            + "data: [DONE]\n\n"
        ).encode("utf-8")

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError("loopback tool SSE server did not stop")


def isolated_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for variable in "PYTHONPATH", "PYTHONHOME":
        environment.pop(variable, None)
    environment.update(
        HOME=str(home),
        USERPROFILE=str(home),
        PYTHONNOUSERSITE="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    return environment


def run_command(
    command: list[str | Path],
    *,
    cwd: Path,
    home: Path | None = None,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved = isolated_environment(home) if home is not None else None
    if environment is not None:
        resolved = environment
    return subprocess.run(
        command,
        cwd=cwd,
        env=resolved,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        input=input_text,
    )


@pytest.fixture(scope="session")
def built_distribution(
    tmp_path_factory: pytest.TempPathFactory,
) -> BuiltDistribution:
    """构建产物只生成一次，供裸安装与完整安装两个 fixture 复用。"""
    build_dir = tmp_path_factory.mktemp("distribution")
    build = run_command(
        ["uv", "build", "--offline", "--out-dir", build_dir],
        cwd=PROJECT_ROOT,
    )
    assert build.returncode == 0, build.stderr

    wheel = build_dir / f"mica_agent-{VERSION}-py3-none-any.whl"
    sdist = build_dir / f"mica_agent-{VERSION}.tar.gz"
    assert wheel.is_file()
    assert sdist.is_file()
    return BuiltDistribution(wheel=wheel, sdist=sdist)


def create_venv(root: Path) -> tuple[Path, Path, Path]:
    """创建全新 Python 3.11 venv，返回 (environment, python, mica 入口)。"""
    environment = root / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    mica = scripts / ("mica.exe" if os.name == "nt" else "mica")
    return environment, python, mica


@pytest.fixture(scope="session")
def bare_distribution(
    tmp_path_factory: pytest.TempPathFactory,
    built_distribution: BuiltDistribution,
) -> InstalledDistribution:
    """裸安装环境：只装 Mica wheel，不安装任何运行依赖（含 httpx）。"""
    root = tmp_path_factory.mktemp("bare-environment")
    home = tmp_path_factory.mktemp("bare-home")
    _environment, python, mica = create_venv(root)

    install = run_command(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            python,
            built_distribution.wheel,
        ],
        cwd=root,
        home=home,
    )
    assert install.returncode == 0, install.stderr
    assert mica.is_file()

    missing_httpx = run_command(
        [python, "-c", "import httpx"],
        cwd=root,
        home=home,
    )
    assert missing_httpx.returncode != 0
    assert "No module named 'httpx'" in missing_httpx.stderr

    return InstalledDistribution(
        python=python,
        mica=mica,
        outside_repository=tmp_path_factory.mktemp("bare-outside-repository"),
        home=home,
    )


@pytest.fixture(scope="session")
def full_distribution(
    tmp_path_factory: pytest.TempPathFactory,
    built_distribution: BuiltDistribution,
) -> InstalledDistribution:
    """完整运行环境：安装 Mica wheel 并复用当前锁定环境的依赖。"""
    root = tmp_path_factory.mktemp("full-environment")
    home = tmp_path_factory.mktemp("full-home")
    _environment, python, mica = create_venv(root)

    install = run_command(
        [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--python",
            python,
            built_distribution.wheel,
        ],
        cwd=root,
        home=home,
    )
    assert install.returncode == 0, install.stderr
    assert mica.is_file()

    site_packages_result = run_command(
        [python, "-c", "import site; print(site.getsitepackages()[0])"],
        cwd=root,
        home=home,
    )
    assert site_packages_result.returncode == 0, site_packages_result.stderr
    site_packages = Path(site_packages_result.stdout.strip())
    locked_site_packages = Path(pytest.__file__).resolve().parent.parent
    (site_packages / "mica-test-runtime.pth").write_text(
        str(locked_site_packages), encoding="utf-8"
    )

    has_runtime_dependencies = run_command(
        [python, "-c", "import httpx; import jsonschema"],
        cwd=root,
        home=home,
    )
    assert has_runtime_dependencies.returncode == 0, has_runtime_dependencies.stderr

    return InstalledDistribution(
        python=python,
        mica=mica,
        outside_repository=tmp_path_factory.mktemp("full-outside-repository"),
        home=home,
    )


def run_installed(
    installed_distribution: InstalledDistribution, command: list[str | Path]
) -> subprocess.CompletedProcess[str]:
    return run_command(
        command,
        cwd=installed_distribution.outside_repository,
        home=installed_distribution.home,
    )


def test_wheel_contains_only_mica_package_and_metadata(
    built_distribution: BuiltDistribution,
) -> None:
    with zipfile.ZipFile(built_distribution.wheel) as wheel:
        members = set(wheel.namelist())
        file_tools_source = wheel.read("mica_agent/file_tools.py").decode("utf-8")

    assert members
    assert {member.split("/", maxsplit=1)[0] for member in members} == {
        "mica_agent",
        f"mica_agent-{VERSION}.dist-info",
    }
    assert "mica_agent/prompts/system.md" in members
    assert 'name="ls"' not in file_tools_source
    assert "MAX_LIST_ENTRIES" not in file_tools_source
    assert "MAX_LIST_OUTPUT_BYTES" not in file_tools_source


def test_sdist_contains_only_mica_source_package(
    built_distribution: BuiltDistribution,
) -> None:
    with tarfile.open(built_distribution.sdist) as sdist:
        members = [Path(member.name) for member in sdist.getmembers()]

    assert members
    assert all(member.parts[0] == PACKAGE_ROOT for member in members)
    packaged_paths = [member.parts[1:] for member in members if len(member.parts) > 1]
    source_paths = [parts for parts in packaged_paths if parts and parts[0] == "src"]
    assert source_paths
    assert all(
        parts == ("src",)
        or (len(parts) > 1 and parts[1] in {"mica_agent", "mica_agent.egg-info"})
        for parts in source_paths
    )
    assert ("src", "mica_agent", "prompts", "system.md") in packaged_paths


def test_installed_distribution_declares_runtime_dependencies(
    bare_distribution: InstalledDistribution,
) -> None:
    result = run_installed(
        bare_distribution,
        [
            bare_distribution.python,
            "-c",
            "import importlib.metadata; print(importlib.metadata.requires('mica-agent'))",
        ],
    )

    assert result.returncode == 0
    assert result.stdout == "['httpx>=0.28.1', 'jsonschema>=4.25.1']\n"
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("arguments", "returncode", "stdout", "stderr", "description"),
    [
        ((), 0, "usage: mica", "", None),
        (("--help",), 0, "usage: mica", "", "A lightweight agent foundation"),
        (("--version",), 0, "mica 0.1.0\n", "", None),
        (("--invalid",), 2, "", "unrecognized arguments", None),
    ],
)
def test_installed_public_entrypoints_match_contract(
    bare_distribution: InstalledDistribution,
    arguments: tuple[str, ...],
    returncode: int,
    stdout: str,
    stderr: str,
    description: str | None,
) -> None:
    console = run_installed(bare_distribution, [bare_distribution.mica, *arguments])
    module = run_installed(
        bare_distribution,
        [bare_distribution.python, "-m", "mica_agent", *arguments],
    )

    for result in console, module:
        assert result.returncode == returncode
        if arguments == ("--version",):
            assert result.stdout == stdout
        elif stdout:
            assert stdout in result.stdout
        else:
            assert result.stdout == ""
        if description is not None:
            assert description in result.stdout
        if stderr:
            assert stderr in result.stderr
        else:
            assert result.stderr == ""
    assert (console.returncode, console.stdout, console.stderr) == (
        module.returncode,
        module.stdout,
        module.stderr,
    )


def test_installed_import_is_silent_side_effect_free_and_outside_repository(
    bare_distribution: InstalledDistribution,
) -> None:
    before_workspace = {
        path.relative_to(bare_distribution.outside_repository)
        for path in bare_distribution.outside_repository.rglob("*")
    }
    before_home = {
        path.relative_to(bare_distribution.home)
        for path in bare_distribution.home.rglob("*")
    }
    repository = str(PROJECT_ROOT).replace("\\", "/")
    environment = str(bare_distribution.python.parent.parent).replace("\\", "/")
    code = (
        "import os\n"
        "import sys\n"
        "import threading\n"
        f"repository = {repository!r}\n"
        f"environment = {environment!r}\n"
        "threads_before = {thread.ident for thread in threading.enumerate()}\n"
        "def reject_side_effects(event, arguments):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'subprocess.Popen'}:\n"
        "        raise RuntimeError(f'prohibited import side effect: {event}')\n"
        "    if event == 'open' and any(flag in str(arguments[1]) for flag in ('w', 'a', 'x', '+')):\n"
        "        raise RuntimeError('attempted file modification')\n"
        "    if event == 'open' and str(arguments[0]).replace('\\\\', '/').startswith(repository):\n"
        "        raise RuntimeError('attempted repository access')\n"
        "sys.addaudithook(reject_side_effects)\n"
        "import mica_agent\n"
        "module_file = str(mica_agent.__file__).replace('\\\\', '/')\n"
        "assert module_file.startswith(environment), module_file\n"
        "assert not module_file.startswith(repository), module_file\n"
        "assert {thread.ident for thread in threading.enumerate()} == threads_before\n"
        "assert not os.path.exists(os.path.join(os.environ['HOME'], '.mica'))\n"
    )
    result = run_installed(bare_distribution, [bare_distribution.python, "-c", code])
    after_workspace = {
        path.relative_to(bare_distribution.outside_repository)
        for path in bare_distribution.outside_repository.rglob("*")
    }
    after_home = {
        path.relative_to(bare_distribution.home)
        for path in bare_distribution.home.rglob("*")
    }

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""
    assert after_workspace == before_workspace
    assert after_home == before_home


FAKE_CLI_ENVIRONMENT = {
    "MICA_API_URL": "https://api.example.test/v1",
    "MICA_API_KEY": "sk-dummy-test-key",
    "MICA_MODEL": "test-model",
}


def cli_environment(home: Path, *, fake_credentials: bool) -> dict[str, str]:
    """隔离 CLI 环境：清除全部 MICA_*，可选注入假的非敏感连接参数。"""
    environment = isolated_environment(home)
    for variable in FAKE_CLI_ENVIRONMENT:
        environment.pop(variable, None)
    if fake_credentials:
        environment.update(FAKE_CLI_ENVIRONMENT)
    return environment


def assert_no_file_side_effects(
    root: Path, before: set[Path], after: set[Path]
) -> None:
    assert after == before, f"unexpected file side effects under {root}"


def snapshot_files(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*")}


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(lambda d: [d.mica, "cli"], id="console"),
        pytest.param(lambda d: [d.python, "-m", "mica_agent", "cli"], id="module"),
    ],
)
def test_installed_cli_exits_cleanly_on_blank_input_eof(
    full_distribution: InstalledDistribution,
    entry: EntryFactory,
) -> None:
    """完整安装态 `mica cli`：空白输入 + EOF 保留一个空健康会话。"""
    dist = full_distribution
    command = entry(dist)
    before_workspace = snapshot_files(dist.outside_repository)
    store = JsonlSessionStore(
        resolve_workspace(dist.outside_repository), dist.home / ".mica"
    )
    before_sessions = SessionService(store).list_sessions().sessions
    before_ids = {session.meta.session_id for session in before_sessions}

    result = run_command(
        command,
        cwd=dist.outside_repository,
        environment=cli_environment(dist.home, fake_credentials=True),
        input_text="\n   \n\t\n",
    )

    after_workspace = snapshot_files(dist.outside_repository)
    after_sessions = SessionService(store).list_sessions().sessions
    after_ids = {session.meta.session_id for session in after_sessions}
    new_ids = after_ids - before_ids
    assert_no_file_side_effects(
        dist.outside_repository,
        before_workspace,
        after_workspace,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""  # 无 traceback、无诊断
    assert "Traceback" not in result.stderr
    assert len(new_ids) == 1
    new_session = next(
        session for session in after_sessions if session.meta.session_id in new_ids
    )
    assert new_session.turns == ()


def test_installed_wheel_restores_session_from_unicode_workspace(
    full_distribution: InstalledDistribution,
) -> None:
    """仓库外已安装 wheel 的成功轮可由 ``--continue`` 恢复。"""
    dist = full_distribution
    workspace = dist.outside_repository / "工作区 with spaces"
    workspace.mkdir()
    before_workspace = snapshot_files(workspace)
    data_root = dist.home / ".mica"
    workspace_identity = resolve_workspace(workspace)
    store = JsonlSessionStore(workspace_identity, data_root)
    service = SessionService(store)
    assert service.list_sessions().sessions == ()

    first_prompt = "first fixed prompt"
    second_prompt = "second fixed prompt"
    fake_key = "sk-p3-06-loopback-only"
    environment = cli_environment(dist.home, fake_credentials=True)
    environment.update(
        {
            "MICA_API_KEY": fake_key,
            "MICA_MODEL": "p3-06-loopback-model",
        }
    )
    assert environment["HOME"] == str(dist.home)
    assert environment["USERPROFILE"] == str(dist.home)
    assert "PYTHONPATH" not in environment

    with LoopbackSSEServer(("first model reply", "second model reply")) as server:
        environment["MICA_API_URL"] = server.base_url
        first = run_command(
            [dist.mica, "cli"],
            cwd=workspace,
            environment=environment,
            input_text=first_prompt + "\n",
        )

        assert first.returncode == 0, first.stderr
        assert first.stdout == "first model reply\n"
        assert first.stderr == ""
        assert "Traceback" not in first.stderr
        assert fake_key not in first.stderr
        assert str(workspace) not in first.stderr

        after_first = service.list_sessions()
        assert after_first.problems == ()
        assert len(after_first.sessions) == 1
        first_session = after_first.sessions[0]
        assert first_session.meta.workspace_path == str(
            workspace_identity.workspace_path
        )
        assert [
            (
                turn.user_message.content,
                turn.assistant_message.content,
            )
            for turn in first_session.turns
        ] == [(first_prompt, "first model reply")]
        assert store.session_directory.is_relative_to(data_root)
        assert len(tuple(store.session_directory.glob("*.jsonl"))) == 1

        second = run_command(
            [dist.mica, "cli", "--continue"],
            cwd=workspace,
            environment=environment,
            input_text=second_prompt + "\n",
        )

        assert second.returncode == 0, second.stderr
        assert second.stdout == "second model reply\n"
        assert second.stderr == ""
        assert "Traceback" not in second.stderr
        assert fake_key not in second.stderr
        assert str(workspace) not in second.stderr

        assert len(server.requests) == 2
        assert server.requests[0]["messages"][0]["role"] == "system"
        assert server.requests[0]["messages"][1:] == [
            {"role": "user", "content": first_prompt},
        ]
        assert server.requests[1]["messages"][0]["role"] == "system"
        assert server.requests[1]["messages"][1:] == [
            {"role": "user", "content": first_prompt},
            {"role": "assistant", "content": "first model reply"},
            {"role": "user", "content": second_prompt},
        ]

    after_second = service.list_sessions()
    assert after_second.problems == ()
    assert len(after_second.sessions) == 1
    second_session = after_second.sessions[0]
    assert second_session.meta.session_id == first_session.meta.session_id
    assert [
        (
            turn.user_message.content,
            turn.assistant_message.content,
        )
        for turn in second_session.turns
    ] == [
        (first_prompt, "first model reply"),
        (second_prompt, "second model reply"),
    ]
    assert snapshot_files(workspace) == before_workspace
    assert not (workspace / ".mica").exists()


def test_installed_wheel_loads_prompt_and_agents_outside_repository(
    full_distribution: InstalledDistribution,
) -> None:
    dist = full_distribution
    case_root = dist.outside_repository / "P5-09 installed case"
    home = case_root / "isolated home"
    workspace = case_root / "工作区 & with spaces"
    home.joinpath(".mica").mkdir(parents=True)
    workspace.mkdir(parents=True)
    global_agents = "# installed global rules"
    workspace_agents = "# installed workspace rules"
    home.joinpath(".mica", "AGENTS.md").write_text(global_agents, encoding="utf-8")
    workspace.joinpath("AGENTS.md").write_text(workspace_agents, encoding="utf-8")
    fake_key = "installed-loopback-key-sentinel"
    environment = cli_environment(home, fake_credentials=True)
    environment.update(MICA_API_KEY=fake_key, MICA_MODEL="installed-test-model")

    with LoopbackSSEServer(("installed response",)) as server:
        environment["MICA_API_URL"] = server.base_url
        result = run_command(
            [dist.mica, "cli"],
            cwd=workspace,
            environment=environment,
            input_text="installed question\n",
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "installed response\n"
    assert result.stderr == ""
    assert len(server.requests) == 1
    messages = server.requests[0]["messages"]
    assert [message["role"] for message in messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    system_prompt = messages[0]["content"]
    escaped_workspace = (
        resolve_workspace(workspace).workspace_path.as_posix().replace("&", "&amp;")
    )
    assert system_prompt.startswith("# Mica\n")
    assert "<environment_context>" in system_prompt
    assert f"<cwd>{escaped_workspace}</cwd>" in system_prompt
    assert f"<root>{escaped_workspace}</root>" in system_prompt
    assert messages[1]["content"] == (
        f'<INSTRUCTIONS source="global">\n{global_agents}\n</INSTRUCTIONS>'
    )
    assert messages[2]["content"] == (
        f'<INSTRUCTIONS source="workspace">\n{workspace_agents}\n</INSTRUCTIONS>'
    )
    assert messages[3] == {"role": "user", "content": "installed question"}

    service = SessionService(
        JsonlSessionStore(resolve_workspace(workspace), home / ".mica")
    )
    raw = next(service.store.session_directory.glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "# Mica",
        "<environment_context>",
        global_agents,
        workspace_agents,
        fake_key,
    ):
        assert forbidden not in raw
    observed_output = result.stdout + result.stderr
    for private in (
        system_prompt,
        global_agents,
        workspace_agents,
        str(workspace),
        fake_key,
        environment["MICA_API_URL"],
    ):
        assert private not in observed_output
    assert server._thread.is_alive() is False


def test_installed_wheel_runs_bash_with_git_bash_in_unicode_workspace(
    full_distribution: InstalledDistribution,
) -> None:
    dist = full_distribution
    workspace = dist.outside_repository / "Bash 工作区 with spaces"
    workspace.mkdir()
    (workspace / "cwd marker.txt").write_text("marker", encoding="utf-8")
    before = snapshot_files(workspace)
    environment = cli_environment(dist.home, fake_credentials=True)
    with LoopbackToolSSEServer(
        tool_name="bash",
        arguments={
            "command": (
                "test -f 'cwd marker.txt' && printf 'git-bash-out\\n'; "
                "printf 'git-bash-err\\n' >&2"
            )
        },
        response_texts=("bash answer",),
    ) as server:
        environment["MICA_API_URL"] = server.base_url
        result = run_command(
            [dist.mica, "cli", "--permission-mode", "default"],
            cwd=workspace,
            environment=environment,
            input_text="run the check\ny\n",
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "bash answer\n"
    assert result.stderr == (
        "permission required for tool bash [y/N]: "
        "tool bash: started\n"
        "tool bash: success\n"
    )
    assert snapshot_files(workspace) == before
    assert len(server.requests) == 2
    assert all(
        tuple(tool["function"]["name"] for tool in request["tools"])
        == ("ask", "bash", "edit", "read", "write")
        for request in server.requests
    )
    tool_result = server.requests[1]["messages"][-1]["content"]
    assert "git-bash-out" in tool_result
    assert "git-bash-err" in tool_result
    assert "Traceback" not in result.stderr


def test_installed_cli_missing_environment_variables_exit_two(
    full_distribution: InstalledDistribution,
) -> None:
    """完整安装态 `mica cli` 缺环境变量：两入口各执行一次，退出码 2、诊断一致。"""
    dist = full_distribution
    console: EntryFactory = lambda d: [d.mica, "cli"]
    module: EntryFactory = lambda d: [d.python, "-m", "mica_agent", "cli"]
    results = [
        run_command(
            console(dist),
            cwd=dist.outside_repository,
            environment=cli_environment(dist.home, fake_credentials=False),
        ),
        run_command(
            module(dist),
            cwd=dist.outside_repository,
            environment=cli_environment(dist.home, fake_credentials=False),
        ),
    ]

    for result in results:
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        for variable in FAKE_CLI_ENVIRONMENT:
            assert variable in result.stderr
    assert results[0].stderr == results[1].stderr  # 两入口诊断一致


def test_installed_wheel_write_and_continue_restore_complete_tool_history(
    full_distribution: InstalledDistribution,
) -> None:
    dist = full_distribution
    workspace = dist.outside_repository / "工具 工作区"
    workspace.mkdir()
    data_root = dist.home / ".mica"
    workspace_identity = resolve_workspace(workspace)
    service = SessionService(JsonlSessionStore(workspace_identity, data_root))
    environment = cli_environment(dist.home, fake_credentials=True)
    fake_key = "sk-loopback-api-key-only"
    sensitive = "sk-p5-08-sensitive-sentinel"
    environment.update(MICA_API_KEY=fake_key, MICA_MODEL="tool-loopback-model")

    with LoopbackToolSSEServer() as server:
        environment["MICA_API_URL"] = server.base_url
        first = run_command(
            [dist.mica, "cli", "--permission-mode", "default"],
            cwd=workspace,
            environment=environment,
            input_text="create the target\ny\n",
        )
        second = run_command(
            [dist.mica, "cli", "--continue", "--permission-mode", "default"],
            cwd=workspace,
            environment=environment,
            input_text="continue without rewriting\n",
        )

    assert first.returncode == 0, first.stderr
    assert first.stdout == "first tool answer\n"
    assert first.stderr == (
        "permission required for tool write [y/N]: "
        "tool write: started\n"
        "tool write: success\n"
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout == "continued answer\n"
    assert second.stderr == ""
    assert (workspace / "目标文件.txt").read_text(encoding="utf-8") == sensitive
    assert {path.relative_to(workspace) for path in workspace.rglob("*")} == {
        Path("目标文件.txt")
    }
    assert not (workspace / ".mica").exists()

    assert len(server.requests) == 3
    assert all(
        tuple(tool["function"]["name"] for tool in request["tools"])
        == ("ask", "bash", "edit", "read", "write")
        for request in server.requests
    )
    assert [message["role"] for message in server.requests[2]["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    tool_call = server.requests[2]["messages"][2]["tool_calls"][0]
    assert tool_call["function"]["name"] == "write"
    assert server.requests[2]["messages"][-1]["content"] == (
        "continue without rewriting"
    )

    listed = service.list_sessions()
    assert listed.problems == ()
    assert len(listed.sessions) == 1
    assert len(listed.sessions[0].turns) == 2
    raw = next(service.store.session_directory.glob("*.jsonl")).read_text(
        encoding="utf-8"
    )
    observed_output = first.stdout + first.stderr + second.stdout + second.stderr + raw
    assert fake_key not in observed_output
    assert sensitive not in observed_output
    assert str(workspace) not in observed_output
    assert '"format_version":3' in raw
    assert server._thread.is_alive() is False
