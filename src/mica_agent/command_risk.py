"""Limited purely lexical risk hint; runs no command, reads no script, proves no safety."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from .workspace_paths import is_sensitive_path_text


class CommandRisk(Enum):
    NO_MATCH = "no_match"
    NEEDS_CONFIRMATION = "needs_confirmation"


@dataclass(frozen=True, slots=True)
class CommandRiskResult:
    risk: CommandRisk
    reason: str | None = None

    @property
    def requires_confirmation(self) -> bool:
        return self.risk is CommandRisk.NEEDS_CONFIRMATION


@dataclass(frozen=True, slots=True)
class _Token:
    text: str
    operator: bool = False


_DANGEROUS = {
    "rm",
    "rmdir",
    "unlink",
    "shred",
    "dd",
    "mkfs",
    "sudo",
    "su",
    "doas",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "chmod",
    "chown",
    "chgrp",
    "eval",
    "source",
    ".",
    "cd",
    "pushd",
    "popd",
}
_WRAPPERS = {"env", "command", "exec", "timeout", "nohup", "xargs"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "cmd", "powershell", "pwsh"}


def _basename(text: str) -> str:
    name = text.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return name.removesuffix(".exe")


def _tokenize(command: str) -> list[_Token]:
    tokens: list[_Token] = []
    word: list[str] = []
    active = False
    quote = ""
    i = 0
    while i < len(command):
        c = command[i]
        if c == "\\" and quote != "'":
            i += 1
            if i == len(command):
                raise ValueError
            escaped = command[i]
            if escaped != "\n":
                if quote == '"' and escaped not in '$`"\\':
                    word.append("\\")
                word.append(escaped)
                active = True
        elif quote:
            if c == quote:
                quote = ""
            elif quote == '"' and c in "$`":
                raise ValueError
            else:
                word.append(c)
        elif c in "\"'":
            quote = c
            active = True
        elif c in "$`(){}*?[]~!#":
            # Expansions, compound syntax and comments are outside this grammar.
            raise ValueError
        elif c in ";|&<>\n" or c.isspace():
            if active:
                tokens.append(_Token("".join(word)))
                word = []
                active = False
            if not c.isspace() or c == "\n":
                op = c
                if i + 1 < len(command) and command[i + 1] == c and c in "|&<>":
                    op += c
                    i += 1
                tokens.append(_Token(op, True))
        else:
            word.append(c)
            active = True
        i += 1
    if quote:
        raise ValueError
    if active:
        tokens.append(_Token("".join(word)))
    return tokens


def _assignment(text: str) -> bool:
    return re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", text) is not None


def _consume_timeout(words: list[str]) -> list[str]:
    index = 1
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option in {"--preserve-status", "--foreground"}:
            index += 1
            continue
        if option in {"-k", "--kill-after"} and index + 1 < len(words):
            index += 2
            continue
        if option == "--":
            index += 1
            break
        raise ValueError
    if index >= len(words) or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)?[smhd]?", words[index]
    ):
        raise ValueError
    index += 1
    if index >= len(words):
        raise ValueError
    return words[index:]


def _consume_xargs(words: list[str]) -> list[str]:
    index = 1
    with_argument = {
        "-d",
        "--delimiter",
        "-E",
        "--eof",
        "-I",
        "--replace",
        "-L",
        "--max-lines",
        "-n",
        "--max-args",
        "-P",
        "--max-procs",
    }
    flags = {
        "-0",
        "--null",
        "-r",
        "--no-run-if-empty",
        "-t",
        "--verbose",
        "--show-limits",
    }
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        if option == "--":
            index += 1
            break
        if option in flags:
            index += 1
            continue
        if option in with_argument:
            if index + 1 >= len(words):
                raise ValueError
            index += 2
            continue
        if any(
            option.startswith(prefix) and option != prefix
            for prefix in ("-d", "-E", "-I", "-L", "-n", "-P")
        ):
            index += 1
            continue
        raise ValueError
    return words[index:]


def _unwrap(words: list[str]) -> list[str]:
    while words:
        if _assignment(words[0]):
            words = words[1:]
            continue
        wrapper = _basename(words[0])
        if wrapper not in _WRAPPERS:
            return words
        if wrapper == "timeout":
            words = _consume_timeout(words)
            continue
        if wrapper == "nohup":
            if len(words) > 1 and words[1] == "--":
                words = words[2:]
            elif len(words) > 1 and words[1].startswith("-"):
                raise ValueError
            else:
                words = words[1:]
            continue
        if wrapper == "xargs":
            words = _consume_xargs(words)
            continue
        words = words[1:]
        while words and words[0].startswith("-"):
            option = words.pop(0)
            if option == "--":
                break
            if wrapper == "env" and option in {"-i", "--ignore-environment"}:
                continue
            if wrapper == "env" and option in {"-u", "--unset"} and words:
                words.pop(0)
                continue
            if wrapper == "command" and option == "-p":
                continue
            raise ValueError
    raise ValueError


def _curl_output_risky(words: list[str]) -> bool:
    index = 1
    while index < len(words):
        word = words[index]
        if word in {"-o", "--output"}:
            if index + 1 >= len(words):
                raise ValueError
            if is_sensitive_path_text(words[index + 1]):
                return True
            index += 2
            continue
        if word.startswith("-o") and word != "-o":
            if is_sensitive_path_text(word[2:]):
                return True
        elif word.startswith("--output=") and is_sensitive_path_text(
            word.split("=", 1)[1]
        ):
            return True
        index += 1
    return False


def _git_risky(words: list[str]) -> bool:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        option = words[i]
        if option in {"-C", "-c", "--git-dir", "--work-tree"}:
            # Changing command context or configuration is not interpreted.
            raise ValueError
        if option not in {"--no-pager", "--paginate", "--no-optional-locks"}:
            raise ValueError
        i += 1
    if i == len(words):
        return False
    subcommand, args = words[i], words[i + 1 :]
    return (
        subcommand in {"push", "clean", "checkout", "restore"}
        or (subcommand == "reset" and any(a.split("=", 1)[0] == "--hard" for a in args))
        or (
            subcommand == "branch"
            and any(
                a == "-D" or (a.startswith("-") and not a.startswith("--") and "D" in a)
                for a in args
            )
        )
    )


def _group_risky(tokens: list[_Token]) -> bool:
    words: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.operator:
            if token.text not in {">", ">>", "<"}:
                raise ValueError
            i += 1
            if i == len(tokens) or tokens[i].operator or not tokens[i].text:
                raise ValueError
            if is_sensitive_path_text(tokens[i].text):
                return True
        else:
            if token.text.isdigit() and i + 1 < len(tokens) and tokens[i + 1].operator:
                raise ValueError  # File-descriptor syntax is outside the grammar.
            words.append(token.text)
        i += 1
    words = _unwrap(words)
    if not words:
        raise ValueError
    name = _basename(words[0])
    if not name:
        raise ValueError
    if name in _DANGEROUS or name.startswith("mkfs."):
        return True
    if name in {
        "if",
        "then",
        "else",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "function",
        "time",
        "coproc",
        "builtin",
    }:
        raise ValueError
    if name == "git" and _git_risky(words):
        return True
    if name == "curl" and _curl_output_risky(words):
        return True
    interpreter = (
        name in _SHELLS
        or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name)
        or name in {"node", "perl", "ruby", "php"}
    )
    if interpreter:
        args = words[1:]
        # Only literal script filenames (and Python -m modules) are understood.
        if name.startswith("python") and len(args) >= 2 and args[0] == "-m":
            return False
        if (
            not args
            or args[0].startswith("-")
            or args[0].startswith("/")
            and name in {"cmd", "powershell", "pwsh"}
        ):
            return True
    if name in {"echo", "printf"}:
        return False  # Literal output is not an executable or a path operand.
    # Visible protected arguments include curl -o, tee, cp, etc.; no side-effect proof.
    return any(is_sensitive_path_text(arg.split("=", 1)[-1]) for arg in words[1:])


def analyze_command(command: str) -> CommandRiskResult:
    """One confirmation for the entire original call, without rewriting it."""
    try:
        if not isinstance(command, str) or not command.strip() or "\x00" in command:
            raise ValueError
        tokens = _tokenize(command)
        if not tokens or (tokens[-1].operator and tokens[-1].text not in {";", "\n"}):
            raise ValueError
        group: list[_Token] = []
        for token in tokens:
            if token.operator and token.text in {";", "\n", "&&", "||", "|", "&"}:
                if token.text == "&":
                    raise ValueError
                if not group:
                    if token.text == "\n":
                        continue
                    raise ValueError
                if _group_risky(group):
                    return CommandRiskResult(
                        CommandRisk.NEEDS_CONFIRMATION, "risk rule matched"
                    )
                group = []
            else:
                group.append(token)
        if group and _group_risky(group):
            return CommandRiskResult(
                CommandRisk.NEEDS_CONFIRMATION, "risk rule matched"
            )
        return CommandRiskResult(CommandRisk.NO_MATCH)
    except ValueError:
        return CommandRiskResult(
            CommandRisk.NEEDS_CONFIRMATION, "unreliable shell structure"
        )


__all__ = ["CommandRisk", "CommandRiskResult", "analyze_command"]
