from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from mica_agent.message_sanitizer import sanitize_presentation_text
from mica_agent.tool_contracts import ToolRequest, ToolResult, ToolResultStatus
from mica_agent.tool_presentation import (
    ToolPresentationKind,
    ToolPresentationSnapshot,
    ToolPresentationStatus,
    build_edit_presentation_detail,
    complete_tool_presentation,
    prepare_tool_presentation,
)
from mica_agent.workspace import resolve_workspace


def test_presentation_text_keeps_http_url_intact() -> None:
    value = "curl https://example.test/file"
    assert sanitize_presentation_text(value) == value

    source = {
        "path": "notes.md",
        "matched": True,
        "lines": [
            {
                "kind": "context",
                "text": "before",
                "old_line": 1,
                "new_line": 1,
            }
        ],
    }
    snapshot = ToolPresentationSnapshot(
        "call-1",
        "edit",
        ToolPresentationKind.EDIT,
        ToolPresentationStatus.PENDING,
        source,
    )
    source["lines"][0]["text"] = "after"

    assert snapshot.payload["lines"][0]["text"] == "before"
    with pytest.raises(FrozenInstanceError):
        snapshot.status = ToolPresentationStatus.SUCCESS  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot.payload["lines"] = ()  # type: ignore[index]


def test_generic_snapshot_cannot_carry_tool_specific_payload() -> None:
    with pytest.raises(ValueError, match="generic presentation payload"):
        ToolPresentationSnapshot(
            "call-1",
            "future_tool",
            ToolPresentationKind.GENERIC,
            ToolPresentationStatus.PENDING,
            {"secret": "hidden"},
        )


def test_snapshot_rejects_recursive_non_json_and_nonfinite_payloads() -> None:
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    for payload in (recursive, {"bad": object()}, {"bad": float("nan")}):
        with pytest.raises((TypeError, ValueError)) as caught:
            ToolPresentationSnapshot(
                "call-1",
                "future_tool",
                ToolPresentationKind.GENERIC,
                ToolPresentationStatus.PENDING,
                payload,
            )
        assert "self" not in str(caught.value)
        assert "nan" not in str(caught.value).lower()


def test_prepare_presentation_covers_builtin_tools_without_exposing_secrets() -> None:
    requests = (
        ToolRequest("read-1", "read", {"path": "docs/guide.md"}),
        ToolRequest(
            "write-1",
            "write",
            {"path": "docs/example.md", "content": "token=sk-secret-value\n标题"},
        ),
        ToolRequest(
            "edit-1",
            "edit",
            {"path": "docs/example.md", "old_text": "old", "new_text": "new"},
        ),
        ToolRequest("bash-1", "bash", {"command": "echo Bearer abc-secret"}),
        ToolRequest("ask-1", "ask", {"question": "继续吗？"}),
        ToolRequest("future-1", "future_tool", {"secret": "do not expose"}),
    )
    kinds = [prepare_tool_presentation(request).kind for request in requests]
    assert kinds == [
        ToolPresentationKind.READ,
        ToolPresentationKind.WRITE,
        ToolPresentationKind.EDIT,
        ToolPresentationKind.BASH,
        ToolPresentationKind.ASK,
        ToolPresentationKind.GENERIC,
    ]
    assert prepare_tool_presentation(requests[0]).payload == {"path": "docs/guide.md"}
    assert prepare_tool_presentation(requests[1]).payload == {
        "path": "docs/example.md",
        "content": "token=[REDACTED]\n标题",
    }
    assert prepare_tool_presentation(requests[3]).payload == {
        "command": "echo [REDACTED]"
    }
    assert prepare_tool_presentation(requests[4]).payload == {"question": "继续吗？"}
    generic = prepare_tool_presentation(requests[5])
    assert generic.payload == {}
    assert generic.message == "tool details are unavailable"
    assert "do not expose" not in repr(generic)


def test_write_presentation_redacts_values_without_reformatting_text() -> None:
    content = '{\n  "api_key": "credential-marker",\n  "nested": {"value": 1}\n}\n'
    request = ToolRequest(
        "write-format",
        "write",
        {"path": "notes.md", "content": content},
    )

    snapshot = prepare_tool_presentation(request)

    assert snapshot.payload["content"] == (
        '{\n  "api_key": "[REDACTED]",\n  "nested": {"value": 1}\n}\n'
    )
    assert request.arguments["content"] == content
    markdown = "# 标题\n\n  保留  空格  \n**粗体**\n"
    ordinary = ToolRequest(
        "write-markdown",
        "write",
        {"path": "notes.md", "content": markdown},
    )
    assert prepare_tool_presentation(ordinary).payload["content"] == markdown


def test_presentation_text_redacts_json_credentials_without_mutating_request() -> None:
    content = '{"api_key":"secret-value","nested":{"token":"Bearer private"}}'
    request = ToolRequest("call-1", "write", {"path": "notes.md", "content": content})

    snapshot = prepare_tool_presentation(request)

    assert snapshot.payload["content"] == (
        '{"api_key":"[REDACTED]","nested":{"token":"[REDACTED]"}}'
    )
    assert request.arguments["content"] == content
    ordinary = ToolRequest(
        "call-2", "write", {"path": "notes.md", "content": "a  b\n# 标题"}
    )
    assert prepare_tool_presentation(ordinary).payload["content"] == "a  b\n# 标题"
    command = ToolRequest(
        "bash-quoted",
        "bash",
        {"command": 'echo {"api_key":"quoted-secret"}'},
    )
    assert prepare_tool_presentation(command).payload["command"] == (
        'echo {"api_key":"[REDACTED]"}'
    )
    numeric = ToolRequest(
        "write-json-number",
        "write",
        {"path": "notes.md", "content": '{"api_key":123,"nested":{"token":false}}'},
    )
    assert prepare_tool_presentation(numeric).payload["content"] == (
        '{"api_key":[REDACTED],"nested":{"token":[REDACTED]}}'
    )


def test_unquoted_credentials_with_spaces_stop_at_explicit_boundaries() -> None:
    cases = (
        ("token = alpha beta", "token = [REDACTED]"),
        ("export API_KEY=alpha beta gamma", "export API_KEY=[REDACTED]"),
        ("API_KEY=alpha; echo beta", "API_KEY=[REDACTED]; echo beta"),
        ("TOKEN=alpha && echo beta", "TOKEN=[REDACTED] && echo beta"),
        ("PASSWORD=alpha, next=value", "PASSWORD=[REDACTED], next=value"),
        (
            '{"token":alpha beta,"next":"value"}',
            '{"token":[REDACTED],"next":"value"}',
        ),
        ('{"token":alpha beta}', '{"token":[REDACTED]}'),
        ("[token=alpha beta]", "[token=[REDACTED]]"),
        ("token=alpha beta\n# 标题  \n", "token=[REDACTED]\n# 标题  \n"),
        (
            "SECRET=sentinel-first sentinel-second; echo safe",
            "SECRET=[REDACTED]; echo safe",
        ),
    )

    for index, (command, expected) in enumerate(cases):
        request = ToolRequest(
            f"bash-unquoted-{index}",
            "bash",
            {"command": command},
        )

        snapshot = prepare_tool_presentation(request)

        assert snapshot.payload["command"] == expected
        assert request.arguments["command"] == command
        assert "sentinel-first" not in repr(snapshot)
        assert "sentinel-second" not in repr(snapshot)


def test_quoted_credentials_are_fully_redacted_without_changing_other_quotes() -> None:
    cases = (
        (
            'export API_KEY="alpha beta gamma"',
            'export API_KEY="[REDACTED]"',
        ),
        (
            "TOKEN='delta epsilon'; PASSWORD='zeta eta'",
            "TOKEN='[REDACTED]'; PASSWORD='[REDACTED]'",
        ),
        (
            r'{"api_key":"alpha \"quoted\" omega","label":"keep me"}',
            r'{"api_key":"[REDACTED]","label":"keep me"}',
        ),
        (
            r'echo "{\"api_key\":\"nested alpha \\\"quoted\\\" omega\"}"',
            r'echo "{\"api_key\":\"[REDACTED]\"}"',
        ),
    )

    sanitized = tuple(
        prepare_tool_presentation(
            ToolRequest(
                f"bash-quoted-{index}",
                "bash",
                {"command": command},
            )
        ).payload["command"]
        for index, (command, _) in enumerate(cases)
    )

    assert sanitized == tuple(expected for _, expected in cases)
    leaked_fragments = (
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
        "zeta",
        "eta",
        "quoted",
        "omega",
    )
    assert all(fragment not in "\n".join(sanitized) for fragment in leaked_fragments)
    ordinary = 'printf \'%s\\n\' "hello world" && echo \'{"label":"keep me"}\''
    snapshot = prepare_tool_presentation(
        ToolRequest("bash-ordinary", "bash", {"command": ordinary})
    )
    assert snapshot.payload["command"] == ordinary


def test_workspace_reparse_point_is_not_retained_in_presentation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")

    snapshot = prepare_tool_presentation(
        ToolRequest("call-link", "read", {"path": "linked/secret.txt"}),
        workspace=resolve_workspace(workspace),
    )

    assert snapshot.payload == {"path": ""}
    assert snapshot.message == "file path is not available"


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "C:/outside.txt",
        ".env",
        "credentials/config.json",
        "docs/sk-secret.txt",
        "docs/token=secret.txt",
    ],
)
def test_invalid_or_sensitive_presentation_path_is_not_retained(path: str) -> None:
    snapshot = prepare_tool_presentation(ToolRequest("call-1", "read", {"path": path}))
    assert snapshot.payload == {"path": ""}
    assert snapshot.message == "file path is not available"


def test_invalid_arguments_keep_the_known_payload_shape() -> None:
    write = prepare_tool_presentation(
        ToolRequest("write-1", "write", {"path": "a.txt"})
    )
    edit = complete_tool_presentation(
        ToolRequest("edit-1", "edit", {"path": "a.txt"}),
        ToolResult("edit-1", "edit", ToolResultStatus.EXECUTION_FAILED, "invalid"),
    )

    assert write.payload == {"path": "a.txt", "content": ""}
    assert edit.payload == {"path": "a.txt", "matched": False, "lines": ()}
    assert edit.status is ToolPresentationStatus.FAILED


def test_terminal_edit_rejects_inconsistent_line_side() -> None:
    with pytest.raises(ValueError, match="new line number"):
        ToolPresentationSnapshot(
            "edit-1",
            "edit",
            ToolPresentationKind.EDIT,
            ToolPresentationStatus.FAILED,
            {
                "path": "notes.md",
                "matched": False,
                "lines": [
                    {"kind": "removed", "text": "old", "old_line": None, "new_line": 1}
                ],
            },
        )


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            "a\nb\n",
            "new\na\nb\n",
            [
                {"kind": "added", "text": "new", "old_line": None, "new_line": 1},
                {"kind": "context", "text": "a", "old_line": 1, "new_line": 2},
                {"kind": "context", "text": "b", "old_line": 2, "new_line": 3},
            ],
        ),
        (
            "a\nb\nc\nd\n",
            "a\nb\nx\ny\nc\nd\n",
            [
                {"kind": "context", "text": "a", "old_line": 1, "new_line": 1},
                {"kind": "context", "text": "b", "old_line": 2, "new_line": 2},
                {"kind": "added", "text": "x", "old_line": None, "new_line": 3},
                {"kind": "added", "text": "y", "old_line": None, "new_line": 4},
                {"kind": "context", "text": "c", "old_line": 3, "new_line": 5},
                {"kind": "context", "text": "d", "old_line": 4, "new_line": 6},
            ],
        ),
        (
            "old\na\nb\n",
            "a\nb\n",
            [
                {"kind": "removed", "text": "old", "old_line": 1, "new_line": None},
                {"kind": "context", "text": "a", "old_line": 2, "new_line": 1},
                {"kind": "context", "text": "b", "old_line": 3, "new_line": 2},
            ],
        ),
        (
            "a\nb\nx\ny\nc\nd\n",
            "a\nb\nc\nd\n",
            [
                {"kind": "context", "text": "a", "old_line": 1, "new_line": 1},
                {"kind": "context", "text": "b", "old_line": 2, "new_line": 2},
                {"kind": "removed", "text": "x", "old_line": 3, "new_line": None},
                {"kind": "removed", "text": "y", "old_line": 4, "new_line": None},
                {"kind": "context", "text": "c", "old_line": 5, "new_line": 3},
                {"kind": "context", "text": "d", "old_line": 6, "new_line": 4},
            ],
        ),
        (
            "a\nold-1\nold-2\nz\n",
            "a\nnew-1\nnew-2\nnew-3\nz\n",
            [
                {"kind": "context", "text": "a", "old_line": 1, "new_line": 1},
                {"kind": "removed", "text": "old-1", "old_line": 2, "new_line": None},
                {"kind": "removed", "text": "old-2", "old_line": 3, "new_line": None},
                {"kind": "added", "text": "new-1", "old_line": None, "new_line": 2},
                {"kind": "added", "text": "new-2", "old_line": None, "new_line": 3},
                {"kind": "added", "text": "new-3", "old_line": None, "new_line": 4},
                {"kind": "context", "text": "z", "old_line": 4, "new_line": 5},
            ],
        ),
        (
            "first\nold\nlast\n",
            "first\nnew\nlast\n",
            [
                {"kind": "context", "text": "first", "old_line": 1, "new_line": 1},
                {"kind": "removed", "text": "old", "old_line": 2, "new_line": None},
                {"kind": "added", "text": "new", "old_line": None, "new_line": 2},
                {"kind": "context", "text": "last", "old_line": 3, "new_line": 3},
            ],
        ),
    ],
)
def test_edit_detail_tracks_old_and_new_context_line_numbers(
    before: str,
    after: str,
    expected: list[dict[str, object]],
) -> None:
    assert build_edit_presentation_detail(before, after, matched=True) == {
        "matched": True,
        "lines": expected,
    }


def test_edit_detail_preserves_added_and_removed_blank_lines() -> None:
    added = build_edit_presentation_detail("a\nb\n", "a\n\nb\n", matched=True)
    removed = build_edit_presentation_detail("a\n\nb\n", "a\nb\n", matched=True)

    assert {"kind": "added", "text": "", "old_line": None, "new_line": 2} in added[
        "lines"
    ]
    assert {
        "kind": "removed",
        "text": "",
        "old_line": 2,
        "new_line": None,
    } in removed["lines"]


def test_failed_edit_keeps_unlocated_diff_without_fake_line_numbers() -> None:
    snapshot = complete_tool_presentation(
        ToolRequest(
            "edit-1",
            "edit",
            {"path": "notes.md", "old_text": "old", "new_text": "new"},
        ),
        ToolResult(
            "edit-1",
            "edit",
            ToolResultStatus.EXECUTION_FAILED,
            "failed",
            presentation={
                "matched": False,
                "lines": [
                    {
                        "kind": "removed",
                        "text": "old",
                        "old_line": None,
                        "new_line": None,
                    },
                    {
                        "kind": "added",
                        "text": "new",
                        "old_line": None,
                        "new_line": None,
                    },
                ],
            },
        ),
    )

    assert snapshot.payload["matched"] is False
    assert snapshot.message == "file was not changed"
    assert snapshot.payload["lines"] == (
        {"kind": "removed", "text": "old", "old_line": None, "new_line": None},
        {"kind": "added", "text": "new", "old_line": None, "new_line": None},
    )


def test_bash_and_ask_final_payloads_keep_results_out_of_generic_fields() -> None:
    bash = complete_tool_presentation(
        ToolRequest("bash-1", "bash", {"command": "echo token=secret"}),
        ToolResult(
            "bash-1",
            "bash",
            ToolResultStatus.EXECUTION_FAILED,
            "command failed",
            {"exit_code": 7, "output": "token=secret"},
        ),
    )
    ask = complete_tool_presentation(
        ToolRequest("ask-1", "ask", {"question": "继续吗？"}),
        ToolResult("ask-1", "ask", ToolResultStatus.SUCCESS, "answer", "继续"),
    )

    assert bash.payload == {
        "command": "echo token=[REDACTED]",
        "output": "token=[REDACTED]",
        "exit_code": 7,
    }
    assert ask.payload == {"question": "继续吗？", "answer": "继续"}
    assert bash.status is ToolPresentationStatus.FAILED
    assert bash.message == "tool execution failed"
    assert ask.status is ToolPresentationStatus.SUCCESS

    invalid_exit = complete_tool_presentation(
        ToolRequest("bash-2", "bash", {"command": "echo ok"}),
        ToolResult(
            "bash-2",
            "bash",
            ToolResultStatus.EXECUTION_FAILED,
            "failed",
            {"exit_code": -1, "output": ""},
        ),
    )
    assert invalid_exit.payload["exit_code"] is None
    cancelled = complete_tool_presentation(
        ToolRequest("read-cancelled", "read", {"path": "notes.md"}),
        ToolResult(
            "read-cancelled",
            "read",
            ToolResultStatus.CANCELLED,
            "operation cancelled",
        ),
    )
    assert cancelled.message == "operation cancelled"


def test_complete_presentation_maps_terminal_status_without_tool_result_data() -> None:
    request = ToolRequest("call-1", "read", {"path": "notes.md"})
    result = ToolResult(
        "call-1", "read", ToolResultStatus.SUCCESS, "file read", "private body"
    )
    snapshot = complete_tool_presentation(request, result)
    assert snapshot.status is ToolPresentationStatus.SUCCESS
    assert snapshot.payload == {"path": "notes.md"}
    assert "private body" not in repr(snapshot)
