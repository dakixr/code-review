import typer


app = typer.Typer(add_completion=False)


@app.callback(invoke_without_command=True)
def _hello(
    ctx: typer.Context,
    name: str = typer.Option("world", "--name", "-n", help="Who to greet."),
) -> None:
    """Say hello."""
    if ctx.invoked_subcommand is None:
        typer.echo(f"Hello, {name}!")


def run() -> None:
    app()


if __name__ == "__main__":
    run()
