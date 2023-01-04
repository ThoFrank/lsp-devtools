import argparse
import json
import logging
import pathlib
import re
from typing import List
from typing import Optional

from rich.console import ConsoleRenderable
from rich.logging import RichHandler
from rich.traceback import Traceback

from lsp_devtools.agent import AgentClient
from lsp_devtools.agent import MESSAGE_TEXT_NOTIFICATION
from lsp_devtools.agent import MessageText

from .filters import LSPFilter


logger = logging.getLogger(__name__)


class RichLSPHandler(RichHandler):

    def __init__(self, level: int, log_time_format="%X", **kwargs):
        super().__init__(
            level=level,
            show_path=False,
            log_time_format=log_time_format,
            omit_repeated_times=False,
            **kwargs,
        )

    def render(
        self, *,
        record: logging.LogRecord,
        traceback: Optional[Traceback],
        message_renderable: "ConsoleRenderable"
    ) -> "ConsoleRenderable":

        # Delegate most of the rendering to the base RichHandler class.
        res = super().render(
            record=record, traceback=traceback, message_renderable=message_renderable
        )

        # Abuse the log level column to display the source of the message,
        source = record.__dict__['source']
        color = "red" if source == "client" else "blue"
        res.columns[1]._cells[0] = f"[bold][{color}]{source.upper()}[/{color}][/bold]"

        return res


def log_raw_message(ls: AgentClient, message: MessageText):
    """Push raw messages through the logging system."""
    logger.info(message.text, extra={'source': message.source})


MESSAGE_PATTERN = re.compile(
    r'^(?:[^\r\n]+\r\n)*'
    + r'Content-Length: (?P<length>\d+)\r\n'
    + r'(?:[^\r\n]+\r\n)*\r\n'
    + r'(?P<body>{.*)',
    re.DOTALL,
)


def log_rpc_message(ls: AgentClient, message: MessageText):
    """Push parsed json-rpc messages through the logging system.

    Originally adatped from the ``data_received`` method on pygls' ``JsonRPCProtocol``
    class.
    """
    data = message.text
    message_buf = ls._client_buf if message.source == 'client' else ls._server_buf

    while len(data):

        # Append the incoming chunk to the message buffer
        message_buf.append(data)

        # Look for the body of the message
        msg = "".join(message_buf)
        found = MESSAGE_PATTERN.fullmatch(msg)

        body = found.group("body") if found else b""
        length = int(found.group("length")) if found else 1

        if len(body) < length:
            # Message is incomplete; bail until more data arrives
            return

        # Message is complete;
        # extract the body and any remaining data,
        # and reset the buffer for the next message
        body, data = body[:length], body[length:]
        message_buf.clear()

        # Log the full message
        logger.info(
            "%s",
            json.loads(body),
            extra={"source": message.source},
        )


def setup_stdout_output():
    handler = RichLSPHandler(level=logging.INFO)

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)


def start_recording(args, extra: List[str]):

    client = AgentClient()

    # Apply message filters
    logger.addFilter(
        LSPFilter(
            message_source=args.message_source,
            include_message_types=args.include_message_types,
            exclude_message_types=args.exclude_message_types,
            include_methods=args.include_methods,
            exclude_methods=args.exclude_methods,
            formatter=args.format_message,
        )
    )

    log_func = log_raw_message if args.capture_raw_output else log_rpc_message
    client.feature(MESSAGE_TEXT_NOTIFICATION)(log_func)

    setup_stdout_output()

    try:
        client.start_ws_client(args.host, args.port)
    except Exception:
        # TODO: Error handling
        raise

def cli(commands: argparse._SubParsersAction):
    cmd: argparse.ArgumentParser = commands.add_parser(
        "record",
        help="record an LSP session, requires the server be wrapped by an agent.",
        description="""\
This command connects to an LSP agent allowing for messages sent
between client and server to be logged.
""",
    )

    connect = cmd.add_argument_group(
        title="connection options",
        description="how to connect to the LSP agent"
    )
    connect.add_argument(
        "--host",
        type=str,
        default="localhost",
        help="the host that is hosting the agent."
    )
    connect.add_argument(
        "-p",
        "--port",
        type=int,
        default=8765,
        help="the port to connect to."
    )

    capture = cmd.add_mutually_exclusive_group()
    capture.add_argument(
        "--capture-raw-output",
        action="store_true",
        help="capture the raw output from client and server."
    )
    capture.add_argument(
        "--capture-rpc-output",
        default=True,
        action="store_true",
        help="capture the rpc messages sent between client and server."
    )

    filter = cmd.add_argument_group(
        title="filter options",
        description=(
            "select which messages to record, mutliple options will be ANDed together. "
            "Does not apply to raw message capture"
        )
    )
    filter.add_argument(
        "--message-source",
        default="both",
        choices=["client", "server", "both"],
        help="only include messages from the given source"
    )
    filter.add_argument(
        "--include-message-type",
        action="append",
        default=[],
        dest="include_message_types",
        choices=["request", "response", "result", "error", "notification"],
        help="only include the given message type(s)"
    )
    filter.add_argument(
        "--exclude-message-type",
        action="append",
        dest="exclude_message_types",
        default=[],
        choices=["request", "response", "result", "error", "notification"],
        help="omit the given message type(s)"
    )
    filter.add_argument(
        "--include-method",
        action="append",
        dest="include_methods",
        default=[],
        metavar="METHOD",
        help="only include the given messages for the given method(s)",
    )
    filter.add_argument(
        "--exclude-method",
        action="append",
        dest="exclude_methods",
        default=[],
        metavar="METHOD",
        help="omit messages for the given method(s)"
    )

    format = cmd.add_argument_group(
        title="formatting options",
        description=(
            "control how the recorded messages are formatted "
            "(does not apply to SQLite output or raw message capture)"
        )
    )
    format.add_argument(
        "-f",
        "--format-message",
        help=(
            "format messages according to given format string, "
            "see example commands above for syntax. "
            "Messages which fail to format will be excluded"
        )
    )

    output = cmd.add_argument_group(
        title="output options",
        description="control where the captured messages are sent to"
    )
    output.add_argument(
        "--to-file",
        default=None,
        metavar="FILE",
        type=pathlib.Path,
        help="save messages to a file"
    )
    output.add_argument(
        "--to-sqlite",
        default=None,
        metavar="FILE",
        type=pathlib.Path,
        help="save messages to a SQLite DB"
    )

    cmd.set_defaults(run=start_recording)


def _enable_pygls_logging():
    pygls_log = logging.getLogger("pygls")
    pygls_log.setLevel(logging.DEBUG)
    pygls_log.addHandler(RichHandler(level=logging.DEBUG))
