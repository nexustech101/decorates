# Building CLI Tools With `decorators.cli`

`decorators.cli` is a lightweight decorator-based framework for building command-line tools from ordinary Python functions.

It supports two usage styles:

1. Full-control bootstrapping with `build_parser()` and `Dispatcher()`
2. A simple script-friendly flow with `@registry.register(...)` and `registry.run()`

This guide focuses on the second style, because it is the fastest way to build a small CLI.

## Quick Start

```python
from decorators.cli import CommandRegistry

cli = CommandRegistry()


@cli.register(
    ops=["-g", "--greet"],
    name="greet",
    description="Greet someone by name",
)
def greet_cli(name: str) -> str:
    return f"Hello, {name}!"


if __name__ == "__main__":
    cli.run()
```

Run it like:

```bash
python test.py greet Alice
python test.py --greet Alice
python test.py g Alice
```

## Core Concepts

### 1. Create a registry

The registry stores cli metadata and handler functions.

```python
from decorators.cli import CommandRegistry

cli = CommandRegistry()
```

### 2. Register clis with decorators

Each decorated function becomes a subcli.

```python
@cli.register(
    ops=["-a", "--add"],
    name="add",
    description="Add two integers",
)
def add_cli(num1: int, num2: int) -> str:
    return str(num1 + num2)
```

Arguments are inferred from annotations:

| Python annotation | CLI behavior |
| --- | --- |
| `str` | required positional string |
| `int` | required positional integer |
| `float` | required positional float |
| `bool` | optional `--flag` |
| `Optional[T]` or defaulted args | optional `--arg value` |

### 3. Run the CLI

`registry.run()` builds the parser, parses arguments, dispatches the cli, and prints any non-`None` return value.

```python
if __name__ == "__main__":
    cli.run()
```

You can also pass explicit arguments during tests:

```python
result = cli.run(["add", "2", "3"], print_result=False)
assert result == "5"
```

## About `ops`

The `ops` field lets you define cli aliases in a compact style:

```python
ops=["-g", "--greet"]
```

With that metadata, all of these forms work:

```bash
python test.py greet Alice
python test.py --greet Alice
python test.py g Alice
```

The canonical cli name is still the `name=` value.

## Standardized Error Handling

The framework does not force one error-handling policy. A clean pattern is to wrap clis with your own decorator:

```python
import functools
import logging
import sys
from typing import Any, Callable


def exception_handler(handle_exit: bool = True, log_errors: bool = True) -> Callable:
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            try:
                return func(*args, **kwargs)
            except KeyboardInterrupt:
                if log_errors:
                    logging.info("Interrupted by user.")
                sys.exit(0)
            except Exception as exc:
                if log_errors:
                    logging.error("cli failed: %s", exc)
                print(f"Error: {exc}", file=sys.stderr)
                if handle_exit:
                    sys.exit(1)
                raise
        return wrapper
    return decorator
```

Usage:

```python
@cli.register(
    ops=["-m", "--multiply"],
    name="multiply",
    description="Multiply two integers",
)
@exception_handler()
def multiply_cli(num1: int, num2: int) -> str:
    return str(num1 * num2)
```

## Listing clis

For lightweight scripts, you can expose a built-in registry view:

```python
@cli.register(
    ops=["-l", "--list"],
    name="list",
    description="List all registered clis",
)
def list_clis() -> None:
    cli.list_clis()
```

## Dependency Injection

For larger apps, `decorators.cli` still supports the lower-level DI container and dispatcher flow.

```python
from decorators.cli import CommandRegistry, DIContainer, Dispatcher, build_parser

registry = CommandRegistry()
container = DIContainer()

container.register(UserService, UserService(...))


@registry.register("create-user", help_text="Create a user")
def create_user(username: str, svc: UserService) -> None:
    svc.create(username)


parser = build_parser(registry, container)
dispatcher = Dispatcher(registry, container)
args = parser.parse_args()

if args.cli:
    cli_args = {k: v for k, v in vars(args).items() if k != "cli"}
    dispatcher.dispatch(args.cli, cli_args)
```

When a parameter's type is registered in the container, it is injected automatically and hidden from the CLI.

## Recommended Project Layout

For a small single-file utility:

```text
my_tool/
  test.py
```

For a larger project:

```text
my_tool/
  app/
    main.py
    clis/
    services/
    persistence/
```

## Reference Example

A full working example using this style lives in the repository root at `test.py`.
