"""Secrets management for Company Kernel.

Moves credentials out of the plaintext `config/secrets.env` into a real store. On macOS the
secure backend is the login keychain (`security`); elsewhere (and in tests) a file backend is
the portable fallback. A names-only index lets us list/export without scanning the keychain.

Positioning (owner decision 2026-06-16): single-tenant private deployment now, but the data
model reserves a `scope` dimension so a future SaaS can isolate per tenant without a rewrite.
`scope` defaults to "default" and nothing enforces isolation yet.

Runtime loading stays backward compatible: bin scripts still source `config/secrets.env`, and now
additionally `eval` the keychain export first, so file values override keychain during migration.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

SERVICE = "ai.openclaw.company-kernel"
DEFAULT_SCOPE = "default"
NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def _root() -> Path:
    env_root = str(os.environ.get("OPENCLAW_COMPANY_KERNEL_ROOT", "") or "").strip()
    base = Path(env_root).expanduser() if env_root else Path(__file__).resolve().parents[1]
    return base.resolve()


def config_dir() -> Path:
    d = _root() / "config"
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_path() -> Path:
    return config_dir() / "secrets-index.json"


def store_path() -> Path:
    return config_dir() / "secrets-store.json"


def legacy_env_path() -> Path:
    return config_dir() / "secrets.env"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ValueError(f"invalid secret name {name!r}: use UPPER_SNAKE_CASE (env var style)")
    return name


def _account(scope: str, name: str) -> str:
    return f"{scope}/{name}"


# --------------------------------------------------------------------------- backends
class FileBackend:
    """Portable fallback: a 0600 JSON keyed by scope/name. Not more secure than secrets.env,
    but cross-OS and the backend tests run against."""

    name = "file"

    def __init__(self, path: Path | None = None):
        self.path = path or store_path()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def set(self, scope: str, name: str, value: str) -> None:
        data = self._load()
        data[_account(scope, name)] = value
        self._save(data)

    def get(self, scope: str, name: str) -> str | None:
        return self._load().get(_account(scope, name))

    def delete(self, scope: str, name: str) -> None:
        data = self._load()
        data.pop(_account(scope, name), None)
        self._save(data)


class KeychainBackend:
    """macOS login keychain via `security` generic-password items."""

    name = "keychain"

    @staticmethod
    def available() -> bool:
        return platform.system() == "Darwin" and shutil.which("security") is not None

    def set(self, scope: str, name: str, value: str) -> None:
        # -U updates if present; -w takes the secret on argv (local single-user box)
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", SERVICE, "-a", _account(scope, name), "-w", value],
            check=True, capture_output=True, text=True,
        )

    def get(self, scope: str, name: str) -> str | None:
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", SERVICE, "-a", _account(scope, name), "-w"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.rstrip("\n")

    def delete(self, scope: str, name: str) -> None:
        subprocess.run(
            ["security", "delete-generic-password", "-s", SERVICE, "-a", _account(scope, name)],
            capture_output=True, text=True,
        )


def resolve_backend(prefer: str | None = None):
    """`prefer` or env COMPANY_KERNEL_SECRETS_BACKEND: keychain | file | auto (default)."""
    choice = (prefer or os.environ.get("COMPANY_KERNEL_SECRETS_BACKEND", "auto")).strip().lower()
    if choice == "file":
        return FileBackend()
    if choice == "keychain":
        return KeychainBackend()
    # auto
    return KeychainBackend() if KeychainBackend.available() else FileBackend()


# --------------------------------------------------------------------------- index (names only)
def _load_index() -> dict:
    p = index_path()
    if not p.exists():
        return {"scopes": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        return {"scopes": {}}
    data.setdefault("scopes", {})
    return data


def _save_index(data: dict) -> None:
    p = index_path()
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        p.chmod(0o600)
    except OSError:
        pass


def _index_add(scope: str, name: str) -> None:
    data = _load_index()
    names = data["scopes"].setdefault(scope, [])
    if name not in names:
        names.append(name)
        names.sort()
    _save_index(data)


def _index_remove(scope: str, name: str) -> None:
    data = _load_index()
    names = data["scopes"].get(scope, [])
    if name in names:
        names.remove(name)
    if not names:
        data["scopes"].pop(scope, None)
    else:
        data["scopes"][scope] = names
    _save_index(data)


def index_names(scope: str) -> list[str]:
    return list(_load_index()["scopes"].get(scope, []))


# --------------------------------------------------------------------------- operations
def set_secret(name: str, value: str, scope: str = DEFAULT_SCOPE, backend=None) -> None:
    name = validate_name(name)
    (backend or resolve_backend()).set(scope, name, value)
    _index_add(scope, name)


def get_secret(name: str, scope: str = DEFAULT_SCOPE, backend=None) -> str | None:
    name = validate_name(name)
    return (backend or resolve_backend()).get(scope, name)


def delete_secret(name: str, scope: str = DEFAULT_SCOPE, backend=None) -> None:
    name = validate_name(name)
    (backend or resolve_backend()).delete(scope, name)
    _index_remove(scope, name)


def mask(value: str | None) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return value[0] + "***"
    return f"{value[:4]}…{value[-2:]} ({len(value)} chars)"


def _sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def export_env_lines(scope: str = DEFAULT_SCOPE, backend=None) -> list[str]:
    backend = backend or resolve_backend()
    lines = []
    for name in index_names(scope):
        value = backend.get(scope, name)
        if value is not None:
            lines.append(f"export {name}={_sh_quote(value)}")
    return lines


def parse_env_file(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if NAME_RE.match(key):
            out[key] = val
    return out


# --------------------------------------------------------------------------- CLI
def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_set(args) -> int:
    value = args.value
    if value is None:
        value = sys.stdin.read().rstrip("\n") if not sys.stdin.isatty() else ""
    if value == "":
        _emit({"ok": False, "error": "empty value; pass --value or pipe it on stdin"})
        return 1
    set_secret(args.key, value, scope=args.scope)
    _emit({"ok": True, "scope": args.scope, "key": args.key, "backend": resolve_backend().name, "stored": mask(value)})
    return 0


def cmd_get(args) -> int:
    value = get_secret(args.key, scope=args.scope)
    if value is None:
        _emit({"ok": False, "scope": args.scope, "key": args.key, "error": "not found"})
        return 1
    if args.reveal:
        print(value)
        return 0
    _emit({"ok": True, "scope": args.scope, "key": args.key, "value": mask(value), "hint": "pass --reveal to print the raw value"})
    return 0


def cmd_list(args) -> int:
    backend = resolve_backend()
    rows = [{"key": n, "value": mask(backend.get(args.scope, n))} for n in index_names(args.scope)]
    _emit({"ok": True, "scope": args.scope, "backend": backend.name, "count": len(rows), "secrets": rows})
    return 0


def cmd_rm(args) -> int:
    delete_secret(args.key, scope=args.scope)
    _emit({"ok": True, "scope": args.scope, "key": args.key, "deleted": True})
    return 0


def cmd_export_env(args) -> int:
    # plain shell, for `eval "$(... export-env)"` in bin scripts — NOT JSON
    for line in export_env_lines(scope=args.scope):
        print(line)
    return 0


def cmd_migrate_file(args) -> int:
    path = Path(args.file) if args.file else legacy_env_path()
    if not path.exists():
        _emit({"ok": False, "error": f"no such file: {path}"})
        return 1
    pairs = parse_env_file(path.read_text(encoding="utf-8"))
    for key, val in pairs.items():
        set_secret(key, val, scope=args.scope)
    _emit({"ok": True, "scope": args.scope, "backend": resolve_backend().name, "imported": sorted(pairs),
           "count": len(pairs), "note": f"values copied into {resolve_backend().name}; {path.name} left in place — delete it once verified"})
    return 0


def _git_tracked(path: Path) -> bool:
    try:
        proc = subprocess.run(["git", "ls-files", "--error-unmatch", str(path)],
                              cwd=str(_root()), capture_output=True, text=True)
        return proc.returncode == 0
    except OSError:
        return False


def cmd_doctor(args) -> int:
    issues, warnings, ok = [], [], []
    backend = resolve_backend()
    ok.append(f"active backend: {backend.name}")
    if not KeychainBackend.available():
        warnings.append("OS keychain unavailable (not macOS or `security` missing) — using file backend; "
                        "secrets at rest are only as protected as the file perms")
    for p in (legacy_env_path(), store_path(), index_path()):
        if not p.exists():
            continue
        mode = p.stat().st_mode & 0o777
        if mode & 0o077:
            issues.append(f"{p.name} is {oct(mode)} — should be 0600 (owner-only). Run: chmod 600 {p}")
        else:
            ok.append(f"{p.name} perms 0600")
        if _git_tracked(p):
            issues.append(f"{p.name} is TRACKED BY GIT — secrets may be committed. Run: git rm --cached {p.name} and add to .gitignore")
    counts = {s: len(n) for s, n in _load_index()["scopes"].items()}
    healthy = not issues
    _emit({"ok": healthy, "backend": backend.name, "secret_counts_by_scope": counts,
           "issues": issues, "warnings": warnings, "good": ok})
    return 0 if healthy else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="company-secrets", description="Manage Company Kernel secrets (keychain/file backend).")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_scope(p):
        p.add_argument("--scope", default=DEFAULT_SCOPE, help="tenant scope (default: default; reserved for future multi-tenant)")

    p = sub.add_parser("set", help="store a secret"); add_scope(p)
    p.add_argument("--key", required=True); p.add_argument("--value", default=None, help="value (or pipe on stdin)")
    p.set_defaults(func=cmd_set)
    p = sub.add_parser("get", help="read a secret (masked unless --reveal)"); add_scope(p)
    p.add_argument("--key", required=True); p.add_argument("--reveal", action="store_true")
    p.set_defaults(func=cmd_get)
    p = sub.add_parser("list", help="list secret names (masked)"); add_scope(p)
    p.set_defaults(func=cmd_list)
    p = sub.add_parser("rm", help="delete a secret"); add_scope(p)
    p.add_argument("--key", required=True)
    p.set_defaults(func=cmd_rm)
    p = sub.add_parser("export-env", help="print `export K=V` lines for shell eval"); add_scope(p)
    p.set_defaults(func=cmd_export_env)
    p = sub.add_parser("migrate-file", help="import a plaintext secrets.env into the store"); add_scope(p)
    p.add_argument("--file", default=None, help="path (default: config/secrets.env)")
    p.set_defaults(func=cmd_migrate_file)
    p = sub.add_parser("doctor", help="check perms, gitignore, backend health")
    p.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, subprocess.CalledProcessError) as exc:
        msg = exc.stderr if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        _emit({"ok": False, "error": str(msg).strip()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
