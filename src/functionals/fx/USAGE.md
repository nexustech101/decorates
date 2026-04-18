# `functionals.fx` Usage

`functionals.fx` is a project management CLI built on:
- `functionals.cli` for command registration, parsing, help, and interactive shell
- `functionals.db` for local control-plane state in `.functionals/fx.db`

It helps you initialize project scaffolds, add module/plugin structure, validate wiring, and track operation history.

## Run `fx`

After installing the package locally:

```bash
pip install -e .
```

Use either entrypoint:

```bash
fx help
python -m functionals.fx help
```

Interactive mode:

```bash
fx --interactive
```

## Quick Start

Initialize a project in a new directory named after the project:

```bash
fx init cli MyService
```

Initialize a project in the current directory:

```bash
fx init cli MyService .
```

Initialize a DB-first project:

```bash
fx init db DataService .
```

Add a CLI module:

```bash
fx module-add cli users .
```

Add a DB module:

```bash
fx module-add db billing .
```

Link an external plugin package to a local alias:

```bash
fx plugin-link my_app.plugins.analytics analytics .
```

Check health and status:

```bash
fx status .
fx health .
fx history 20 .
```

## Scaffold Output

`fx init cli` creates:

```text
app.py
plugins/__init__.py
.functionals/fx.db
```

`fx init db` creates:

```text
models.py
plugins/__init__.py
.functionals/fx.db
```

`fx module-add cli <name>` creates:

```text
plugins/<name>/__init__.py
plugins/<name>/<name>.py
```

`fx module-add db <name>` creates:

```text
plugins/<name>/__init__.py
plugins/<name>/models.py
```

`fx plugin-link <package_path> <alias>` creates:

```text
plugins/<alias>/__init__.py
```

with:

```python
from <package_path> import *
```

## Command Reference

- `fx init [cli|db] [project_name] [root] [--force]`
  - Initialize project scaffold + project record.
  - Backward compatibility: `fx init <project_name>` defaults to `cli`.
- `fx status [root]`
  - Show scaffold, registry, and local plugin alignment.
- `fx module-add <cli|db> <module_name> [root] [--force]`
  - Scaffold a module and register it.
- `fx module-list [root]`
  - List registered modules.
- `fx plugin-link <package_path> [alias] [root] [--force]`
  - Create local plugin alias shim and register it.
- `fx plugin-list [root]`
  - List linked plugins.
- `fx health [root]`
  - Validate core scaffold files and plugin importability.
- `fx history [limit] [root]`
  - Show recent `fx` operations recorded in local state DB.

## Local State

`fx` stores metadata in:

```text
.functionals/fx.db
```

Tracked entities include:
- project metadata
- module registry
- plugin links
- command operation history

## Notes

- For `init`, when `root` is omitted and `project_name` is provided, `root` defaults to `project_name`.
- For other commands, `root` defaults to `.`.
- `module_name` and `alias` must be valid Python identifiers (hyphens are normalized to underscores).
- `--force` overwrites scaffold files where supported.
- `health` imports plugins from the local `plugins` package to verify runtime loadability.
