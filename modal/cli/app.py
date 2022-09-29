import asyncio
import inspect
import sys
import traceback
from typing import List, Optional, Tuple

import typer
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt

from modal.functions import _Function
from modal.stub import _Stub
from modal_utils.async_utils import synchronizer
from modal_utils.package_utils import import_stub_by_ref

app_cli = typer.Typer(name="app", help="Manage running and deployed apps.", no_args_is_help=True)


@app_cli.command("deploy", help="Deploy a Modal stub as an application.")
def deploy(
    stub_ref: str = typer.Argument(..., help="Path to a Python file with a stub."),
    name: str = typer.Option(None, help="Name of the deployment."),
):
    try:
        stub = import_stub_by_ref(stub_ref)
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    if name is None:
        name = stub.name

    res = stub.deploy(name=name)
    if inspect.iscoroutine(res):
        asyncio.run(res)


def make_function_panel(idx: int, tag: str, function: _Function, stub: _Stub) -> Panel:
    items = [
        f"- {i}"
        for i in [*function._mounts, function._image, *function._secrets, *function._shared_volumes.values()]
        if i not in [stub._client_mount, *stub._function_mounts.values()]
    ]
    return Panel(
        Markdown("\n".join(items)),
        title=f"[bright_magenta]{idx}. [/bright_magenta][bold]{tag}[/bold]",
        title_align="left",
    )


def choose_function(stub: _Stub, functions: List[Tuple[str, _Function]], console: Console):
    if len(functions) == 0:
        return None
    elif len(functions) == 1:
        return functions[0][1]

    function_panels = [make_function_panel(idx, tag, obj, stub) for idx, (tag, obj) in enumerate(functions)]

    renderable = Panel(Group(*function_panels))
    console.print(renderable)

    choice = Prompt.ask(
        "[yellow] Pick a function definition to create a corresponding shell: [/yellow]",
        choices=[str(i) for i in range(len(functions))],
        default="0",
        show_default=False,
    )

    return functions[int(choice)][1]


@app_cli.command("shell", no_args_is_help=True)
def shell(
    stub_ref: str = typer.Argument(..., help="Path to a Python file with a stub."),
    function_name: Optional[str] = typer.Argument(
        default=None,
        help="Name of the Modal function to run. If unspecified, Modal will prompt you for a function if running in interactive mode.",
    ),
    cmd: str = typer.Option(default="/bin/bash", help="Command to run inside the Modal image."),
):
    """Run an interactive shell inside a Modal image.\n
    **Examples:**\n
    \n
    - Start a bash shell using the spec for `my_function` in your stub:\n
    ```bash\n
    modal app shell hello_world.py my_function \n
    ```\n
    Note that you can select the function interactively if you omit the function name.\n
    \n
    - Start a `python` shell: \n
    ```bash\n
    modal app shell hello_world.py --cmd=python \n
    ```\n
    """
    try:
        stub = import_stub_by_ref(stub_ref)
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    console = Console()

    if not console.is_terminal:
        print("`modal app shell` can only be run from a terminal.")
        sys.exit(1)

    _stub = synchronizer._translate_in(stub)
    functions = {tag: obj for tag, obj in _stub._blueprint.items() if isinstance(obj, _Function)}

    if function_name is not None:
        if function_name not in functions:
            print(f"Function {function_name} not found in stub.")
            sys.exit(1)
        function = functions[function_name]
    else:
        function = choose_function(_stub, list(functions.items()), console)

    if function is None:
        stub.interactive_shell(cmd)
    else:
        stub.interactive_shell(
            cmd,
            mounts=function._mounts,
            shared_volumes=function._shared_volumes,
            image=function._image,
            secrets=function._secrets,
        )
