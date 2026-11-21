"""Standalone sandbox-side observer for public Click switches. No task or evaluator imports."""
import json
import os
from pathlib import Path
import shlex


def option_used(option, args):
    flags = set(option.opts + getattr(option, "secondary_opts", []))
    return any(arg.split("=", 1)[0] in flags for arg in args)


def modes_for(axis, cli, args, kwargs, runner_env, result):
    if isinstance(args, str):
        args = shlex.split(args)
    args = list(args or [])
    # A nested invocation uses its leaf's configuration. Ambiguous group parsing is unobserved.
    while getattr(cli, "commands", None):
        if not args or args[0] not in cli.commands:
            return set()
        cli = cli.commands[args.pop(0)]
    context = getattr(cli, "context_settings", None) or {}
    help_flags = set(context.get("help_option_names", ["--help"]))
    help_requested = bool(help_flags.intersection(args))
    output = result.output
    env = {**os.environ, **runner_env, **(kwargs.get("env") or {})}
    if axis == "add_help_option":
        if result.exit_code == 2 and "Usage:" in output and "Error:" in output and not help_requested:
            return {bool(cli.add_help_option)}
        return set()
    modes = set()
    for p in getattr(cli, "params", ()):
        if axis == "count" and help_requested and result.exit_code == 0:
            bounded = any(getattr(p.type, k, None) is not None for k in ("min", "max"))
            if (getattr(p, "count", False) or bounded) and any(
                flag in line.split() or flag in line.replace(",", " ").split()
                for line in output.splitlines() for flag in p.opts
            ):
                modes.add(bool(getattr(p, "count", False)))
        if axis == "case_insensitive" and not help_requested and hasattr(p.type, "case_sensitive"):
            supplied = (bool(len(args) == 1 and not args[0].startswith("-")) if p.param_type_name == "argument"
                        else option_used(p, args))
            if supplied:
                modes.add(not p.type.case_sensitive)
        if axis == "completion_case_sensitive" and hasattr(p.type, "case_sensitive"):
            name = kwargs.get("prog_name") or getattr(cli, "name", None)
            if name and env.get("_" + name.replace("-", "_").upper() + "_COMPLETE") == "bash_complete":
                words = shlex.split(env.get("COMP_WORDS", ""))
                index = int(env.get("COMP_CWORD", "-1"))
                if 1 < index <= len(words) and words[index - 1] in p.opts:
                    modes.add(bool(p.type.case_sensitive))
        if axis == "allow_from_autoenv" and not help_requested and hasattr(p, "allow_from_autoenv"):
            prefix = kwargs.get("auto_envvar_prefix", context.get("auto_envvar_prefix"))
            explicit = getattr(p, "envvar", None) or []
            explicit = [explicit] if isinstance(explicit, str) else explicit
            if (prefix and p.name and not option_used(p, args) and not any(env.get(k) for k in explicit)
                    and env.get(f"{prefix}_{p.name}".upper().replace("-", "_")) is not None):
                modes.add(bool(p.allow_from_autoenv))
    return modes


def install(axis, sink):
    import click
    from click.testing import CliRunner

    if not Path(click.__file__).resolve().is_relative_to("/workspace"):
        return
    invoke = CliRunner.invoke

    def watch(self, cli, *pos, **kwargs):
        result = invoke(self, cli, *pos, **kwargs)
        try:
            args = pos[0] if pos else kwargs.get("args")
            options = dict(kwargs)
            if len(pos) >= 3:
                options["env"] = pos[2]
            states = modes_for(axis, cli, args, options, getattr(self, "env", {}) or {}, result)
            with open(sink, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(sorted(states)) + "\n")
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        return result

    CliRunner.invoke = watch
