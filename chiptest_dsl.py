#!/usr/bin/env python3
"""
A lightweight DSL runtime for chip testing via PyVISA.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

try:
    import pyvisa  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pyvisa = None


# -----------------------------
# AST node definitions
# -----------------------------


@dataclass
class Connect:
    alias: str
    resource: str
    line_no: int


@dataclass
class Write:
    alias: str
    command: str
    line_no: int


@dataclass
class Query:
    variable: str
    alias: str
    command: str
    line_no: int


@dataclass
class Wait:
    seconds_expr: str
    line_no: int


@dataclass
class Let:
    variable: str
    expr: str
    line_no: int


@dataclass
class Assert:
    expr: str
    message: str | None
    line_no: int


@dataclass
class Repeat:
    count_expr: str
    body: list["Statement"]
    line_no: int


@dataclass
class Print:
    payload: str
    is_expr: bool
    line_no: int


@dataclass
class IfElse:
    condition_expr: str
    then_body: list["Statement"]
    else_body: list["Statement"]
    line_no: int


@dataclass
class Retry:
    attempts_expr: str
    delay_expr: str | None
    body: list["Statement"]
    line_no: int


@dataclass
class Metric:
    name: str
    expr: str
    line_no: int


@dataclass
class Report:
    fmt: str
    path: str
    line_no: int


Statement = (
    Connect
    | Write
    | Query
    | Wait
    | Let
    | Assert
    | Repeat
    | Print
    | IfElse
    | Retry
    | Metric
    | Report
)


# -----------------------------
# Parser
# -----------------------------


class DSLParseError(ValueError):
    pass


def parse_dsl(source: str) -> list[Statement]:
    lines: list[tuple[int, str]] = []
    for idx, raw in enumerate(source.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append((idx, line))

    cursor = 0

    def parse_block(end_tokens: set[str] | None = None) -> tuple[list[Statement], str | None]:
        nonlocal cursor
        stop_tokens = end_tokens or set()
        block: list[Statement] = []

        while cursor < len(lines):
            line_no, line = lines[cursor]
            upper = line.upper()

            if upper in stop_tokens:
                cursor += 1
                return block, upper

            if upper in {"END", "ELSE"}:
                raise DSLParseError(f"Line {line_no}: unexpected {upper}")

            if upper.startswith("IF "):
                condition_expr = line[2:].strip()
                if not condition_expr:
                    raise DSLParseError(f"Line {line_no}: IF requires condition expression")
                cursor += 1
                then_body, terminator = parse_block({"ELSE", "END"})
                if terminator is None:
                    raise DSLParseError(f"Line {line_no}: missing END for IF block")
                if terminator == "ELSE":
                    else_body, end_terminator = parse_block({"END"})
                    if end_terminator != "END":
                        raise DSLParseError(f"Line {line_no}: missing END for IF/ELSE block")
                else:
                    else_body = []
                block.append(
                    IfElse(
                        condition_expr=condition_expr,
                        then_body=then_body,
                        else_body=else_body,
                        line_no=line_no,
                    )
                )
                continue

            if upper.startswith("REPEAT "):
                count_expr = line[7:].strip()
                if not count_expr:
                    raise DSLParseError(f"Line {line_no}: REPEAT requires count expression")
                cursor += 1
                body, terminator = parse_block({"END"})
                if terminator != "END":
                    raise DSLParseError(f"Line {line_no}: missing END for REPEAT block")
                block.append(Repeat(count_expr=count_expr, body=body, line_no=line_no))
                continue

            if upper.startswith("RETRY "):
                match = re.match(
                    r"^RETRY\s+(.+?)(?:\s+DELAY\s+(.+))?$", line, flags=re.IGNORECASE
                )
                if not match:
                    raise DSLParseError(
                        f"Line {line_no}: RETRY syntax is RETRY <attempts_expr> [DELAY <seconds_expr>]"
                    )
                attempts_expr = match.group(1).strip()
                delay_expr = match.group(2).strip() if match.group(2) else None
                if not attempts_expr:
                    raise DSLParseError(f"Line {line_no}: RETRY requires attempts expression")
                cursor += 1
                body, terminator = parse_block({"END"})
                if terminator != "END":
                    raise DSLParseError(f"Line {line_no}: missing END for RETRY block")
                block.append(
                    Retry(
                        attempts_expr=attempts_expr,
                        delay_expr=delay_expr,
                        body=body,
                        line_no=line_no,
                    )
                )
                continue

            block.append(_parse_statement(line_no, line))
            cursor += 1

        return block, None

    parsed, terminator = parse_block()
    if terminator is not None or cursor != len(lines):
        raise DSLParseError("Parser ended unexpectedly")
    return parsed


def _parse_statement(line_no: int, line: str) -> Statement:
    cmd = line.split(maxsplit=1)[0].upper()

    if cmd == "CONNECT":
        parts = shlex.split(line)
        if len(parts) != 3:
            raise DSLParseError(f"Line {line_no}: CONNECT syntax is CONNECT <alias> \"<resource>\"")
        return Connect(alias=parts[1], resource=parts[2], line_no=line_no)

    if cmd == "WRITE":
        parts = shlex.split(line)
        if len(parts) < 3:
            raise DSLParseError(f"Line {line_no}: WRITE syntax is WRITE <alias> \"<scpi>\"")
        return Write(alias=parts[1], command=" ".join(parts[2:]), line_no=line_no)

    if cmd == "QUERY":
        parts = shlex.split(line)
        if len(parts) < 5 or parts[2].upper() != "FROM":
            raise DSLParseError(
                f"Line {line_no}: QUERY syntax is QUERY <var> FROM <alias> \"<scpi>\""
            )
        return Query(
            variable=parts[1],
            alias=parts[3],
            command=" ".join(parts[4:]),
            line_no=line_no,
        )

    if cmd == "WAIT":
        expr = line[4:].strip()
        if not expr:
            raise DSLParseError(f"Line {line_no}: WAIT requires seconds expression")
        return Wait(seconds_expr=expr, line_no=line_no)

    if cmd == "LET":
        match = re.match(r"^LET\s+([A-Za-z_]\w*)\s*=\s*(.+)$", line, flags=re.IGNORECASE)
        if not match:
            raise DSLParseError(f"Line {line_no}: LET syntax is LET <var> = <expression>")
        return Let(variable=match.group(1), expr=match.group(2), line_no=line_no)

    if cmd == "ASSERT":
        payload = line[6:].strip()
        if not payload:
            raise DSLParseError(f"Line {line_no}: ASSERT requires expression")
        message = None
        split = re.search(r"\s+MESSAGE\s+", payload, flags=re.IGNORECASE)
        if split:
            expr = payload[: split.start()].strip()
            raw_message = payload[split.end() :].strip()
            message = _parse_quoted_string(line_no, raw_message, "ASSERT MESSAGE")
        else:
            expr = payload
        if not expr:
            raise DSLParseError(f"Line {line_no}: ASSERT requires expression before MESSAGE")
        return Assert(expr=expr, message=message, line_no=line_no)

    if cmd == "PRINT":
        payload = line[5:].strip()
        if not payload:
            raise DSLParseError(f"Line {line_no}: PRINT requires string literal or expression")
        if payload[0] in {"'", '"'}:
            text = _parse_quoted_string(line_no, payload, "PRINT")
            return Print(payload=text, is_expr=False, line_no=line_no)
        return Print(payload=payload, is_expr=True, line_no=line_no)

    if cmd == "METRIC":
        match = re.match(r"^METRIC\s+([A-Za-z_]\w*)\s*=\s*(.+)$", line, flags=re.IGNORECASE)
        if not match:
            raise DSLParseError(f"Line {line_no}: METRIC syntax is METRIC <name> = <expression>")
        return Metric(name=match.group(1), expr=match.group(2), line_no=line_no)

    if cmd == "REPORT":
        parts = shlex.split(line)
        if len(parts) != 3:
            raise DSLParseError(
                f"Line {line_no}: REPORT syntax is REPORT <JSON|CSV> \"<output_path>\""
            )
        fmt = parts[1].upper()
        if fmt not in {"JSON", "CSV"}:
            raise DSLParseError(f"Line {line_no}: REPORT format must be JSON or CSV")
        return Report(fmt=fmt, path=parts[2], line_no=line_no)

    raise DSLParseError(f"Line {line_no}: unknown command '{cmd}'")


def _parse_quoted_string(line_no: int, raw: str, context: str) -> str:
    try:
        value = ast.literal_eval(raw)
    except Exception as exc:
        raise DSLParseError(f"Line {line_no}: invalid string for {context}: {raw}") from exc
    if not isinstance(value, str):
        raise DSLParseError(f"Line {line_no}: {context} must be a quoted string")
    return value


# -----------------------------
# VISA adapter layer
# -----------------------------


class SessionAdapter(Protocol):
    def open_resource(self, resource: str) -> Any:
        ...

    def write(self, session: Any, command: str) -> None:
        ...

    def query(self, session: Any, command: str) -> str:
        ...

    def close(self, session: Any) -> None:
        ...

    def shutdown(self) -> None:
        ...


class PyVISAAdapter:
    def __init__(self) -> None:
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed. Use --mock or install pyvisa.")
        self._rm = pyvisa.ResourceManager()

    def open_resource(self, resource: str) -> Any:
        return self._rm.open_resource(resource)

    def write(self, session: Any, command: str) -> None:
        session.write(command)

    def query(self, session: Any, command: str) -> str:
        return str(session.query(command)).strip()

    def close(self, session: Any) -> None:
        session.close()

    def shutdown(self) -> None:
        self._rm.close()


@dataclass
class MockSession:
    resource: str
    writes: list[str]


class MockAdapter:
    def open_resource(self, resource: str) -> MockSession:
        return MockSession(resource=resource, writes=[])

    def write(self, session: MockSession, command: str) -> None:
        session.writes.append(command)

    def query(self, session: MockSession, command: str) -> str:
        upper = command.upper()
        if "*IDN?" in upper:
            return f"MOCK,INSTR,{session.resource},1.0"
        if "MEAS:CURR" in upper:
            return "7.5e-07"
        if "MEAS:VOLT" in upper:
            return "1.8000"
        return "0"

    def close(self, session: MockSession) -> None:
        _ = session

    def shutdown(self) -> None:
        return None


# -----------------------------
# Runtime
# -----------------------------


class DSLError(RuntimeError):
    pass


class _TemplateDict(dict[str, Any]):
    def __missing__(self, key: str) -> Any:
        raise KeyError(f"Variable '{key}' is not defined")


SAFE_GLOBALS: dict[str, Any] = {
    "__builtins__": {},
    "abs": abs,
    "bool": bool,
    "dict": dict,
    "list": list,
    "min": min,
    "max": max,
    "sum": sum,
    "len": len,
    "int": int,
    "float": float,
    "str": str,
    "tuple": tuple,
    "round": round,
    "math": math,
}


class DSLRunner:
    def __init__(self, adapter: SessionAdapter) -> None:
        self._adapter = adapter
        self._sessions: dict[str, Any] = {}
        self.variables: dict[str, Any] = {}
        self.metrics: dict[str, Any] = {}

    def run(self, program: list[Statement]) -> None:
        try:
            self._run_block(program)
        finally:
            for session in self._sessions.values():
                try:
                    self._adapter.close(session)
                except Exception:
                    pass
            self._adapter.shutdown()

    def _run_block(self, block: list[Statement]) -> None:
        for stmt in block:
            if isinstance(stmt, Connect):
                self._exec_connect(stmt)
            elif isinstance(stmt, Write):
                self._exec_write(stmt)
            elif isinstance(stmt, Query):
                self._exec_query(stmt)
            elif isinstance(stmt, Wait):
                self._exec_wait(stmt)
            elif isinstance(stmt, Let):
                self._exec_let(stmt)
            elif isinstance(stmt, Assert):
                self._exec_assert(stmt)
            elif isinstance(stmt, Print):
                self._exec_print(stmt)
            elif isinstance(stmt, Repeat):
                self._exec_repeat(stmt)
            elif isinstance(stmt, IfElse):
                self._exec_if_else(stmt)
            elif isinstance(stmt, Retry):
                self._exec_retry(stmt)
            elif isinstance(stmt, Metric):
                self._exec_metric(stmt)
            elif isinstance(stmt, Report):
                self._exec_report(stmt)
            else:  # pragma: no cover - unreachable by type
                raise DSLError(f"Unsupported statement type: {type(stmt)!r}")

    def _exec_connect(self, stmt: Connect) -> None:
        if stmt.alias in self._sessions:
            raise DSLError(f"Line {stmt.line_no}: alias '{stmt.alias}' is already connected")
        self._sessions[stmt.alias] = self._adapter.open_resource(stmt.resource)

    def _exec_write(self, stmt: Write) -> None:
        session = self._require_session(stmt.alias, stmt.line_no)
        self._adapter.write(session, self._format(stmt.command, stmt.line_no))

    def _exec_query(self, stmt: Query) -> None:
        session = self._require_session(stmt.alias, stmt.line_no)
        response = self._adapter.query(session, self._format(stmt.command, stmt.line_no))
        self.variables[stmt.variable] = response

    def _exec_wait(self, stmt: Wait) -> None:
        seconds = float(self._eval_expr(stmt.seconds_expr, stmt.line_no))
        if seconds < 0:
            raise DSLError(f"Line {stmt.line_no}: WAIT seconds cannot be negative")
        time.sleep(seconds)

    def _exec_let(self, stmt: Let) -> None:
        self.variables[stmt.variable] = self._eval_expr(stmt.expr, stmt.line_no)

    def _exec_assert(self, stmt: Assert) -> None:
        ok = bool(self._eval_expr(stmt.expr, stmt.line_no))
        if not ok:
            message = stmt.message or f"Assertion failed: {stmt.expr}"
            raise AssertionError(f"Line {stmt.line_no}: {message}")

    def _exec_print(self, stmt: Print) -> None:
        if stmt.is_expr:
            value = self._eval_expr(stmt.payload, stmt.line_no)
        else:
            value = self._format(stmt.payload, stmt.line_no)
        print(value)

    def _exec_repeat(self, stmt: Repeat) -> None:
        count = int(self._eval_expr(stmt.count_expr, stmt.line_no))
        if count < 0:
            raise DSLError(f"Line {stmt.line_no}: REPEAT count cannot be negative")

        marker = object()
        previous_i = self.variables.get("i", marker)
        for i in range(count):
            self.variables["i"] = i
            self._run_block(stmt.body)

        if previous_i is marker:
            self.variables.pop("i", None)
        else:
            self.variables["i"] = previous_i

    def _exec_if_else(self, stmt: IfElse) -> None:
        if bool(self._eval_expr(stmt.condition_expr, stmt.line_no)):
            self._run_block(stmt.then_body)
        else:
            self._run_block(stmt.else_body)

    def _exec_retry(self, stmt: Retry) -> None:
        attempts = int(self._eval_expr(stmt.attempts_expr, stmt.line_no))
        if attempts <= 0:
            raise DSLError(f"Line {stmt.line_no}: RETRY attempts must be >= 1")
        delay = 0.0
        if stmt.delay_expr is not None:
            delay = float(self._eval_expr(stmt.delay_expr, stmt.line_no))
        if delay < 0:
            raise DSLError(f"Line {stmt.line_no}: RETRY delay cannot be negative")

        last_exc: Exception | None = None
        for idx in range(attempts):
            try:
                self._run_block(stmt.body)
                self.variables["retry_attempt"] = idx + 1
                return
            except Exception as exc:  # noqa: PERF203
                last_exc = exc
                self.variables["retry_attempt"] = idx + 1
                self.variables["last_error"] = str(exc)
                if idx == attempts - 1:
                    break
                if delay > 0:
                    time.sleep(delay)
        assert last_exc is not None
        raise DSLError(
            f"Line {stmt.line_no}: RETRY failed after {attempts} attempts: {last_exc}"
        ) from last_exc

    def _exec_metric(self, stmt: Metric) -> None:
        self.metrics[stmt.name] = self._eval_expr(stmt.expr, stmt.line_no)

    def _exec_report(self, stmt: Report) -> None:
        raw_path = self._format(stmt.path, stmt.line_no)
        output_path = Path(raw_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if stmt.fmt == "JSON":
            payload = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "metrics": self.metrics,
                "variables": self.variables,
            }
            output_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str),
                encoding="utf-8",
            )
            return

        with output_path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["metric", "value"])
            for key in sorted(self.metrics):
                writer.writerow([key, self.metrics[key]])

    def _format(self, template: str, line_no: int) -> str:
        try:
            return template.format_map(_TemplateDict(self.variables))
        except KeyError as exc:
            raise DSLError(f"Line {line_no}: {exc}") from exc

    def _eval_expr(self, expr: str, line_no: int) -> Any:
        try:
            return eval(expr, SAFE_GLOBALS, dict(self.variables))
        except Exception as exc:
            raise DSLError(f"Line {line_no}: expression error in '{expr}': {exc}") from exc

    def _require_session(self, alias: str, line_no: int) -> Any:
        if alias not in self._sessions:
            raise DSLError(f"Line {line_no}: unknown device alias '{alias}'")
        return self._sessions[alias]


# -----------------------------
# CLI
# -----------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run chip-test DSL scripts using PyVISA or a mock backend."
    )
    parser.add_argument("script", help="Path to DSL script")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use mock backend (no physical instrument required)",
    )
    parser.add_argument(
        "--dump-vars",
        action="store_true",
        help="Print final variable map after execution",
    )
    args = parser.parse_args(argv)

    source = Path(args.script).read_text(encoding="utf-8")
    program = parse_dsl(source)
    runner = DSLRunner(adapter=MockAdapter() if args.mock else PyVISAAdapter())

    try:
        runner.run(program)
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    if args.dump_vars:
        print("--- Final Variables ---")
        for key in sorted(runner.variables):
            print(f"{key} = {runner.variables[key]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
