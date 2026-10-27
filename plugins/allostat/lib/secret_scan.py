"""Secret-shaped path detection — shared denylist + post-build inspection gate.

RB-4 (Codex audit P0-1 / P0-2): secret-shaped files (credentials, private
keys, .env files) could previously end up in the customer bundle
(wrapper/scripts/make_bundle.py) and in the staging deploy tarball
(server/scripts/deploy_staging.sh) with no dedicated safeguard beyond
whatever exclusion list each script happened to already carry.

This module is the single source of truth for "does this path look like a
secret?" so both build points share one implementation instead of two glob
lists that can silently drift apart.

Two layers, both backed by the same `is_secret_shaped()` predicate:
  1. PREVENTION — callers filter file lists before writing the artifact.
     make_bundle.py's `_excluded()` calls `is_secret_shaped()` directly.
     deploy_staging.sh's `tar --exclude` mirrors the same denylist items
     (tar has no Python hook to call this module during assembly), and
     is deliberately blunt/conservative there since it is a shell glob.
  2. DETECTION (fail-safe) — `scan_zip` / `scan_tar` / `scan_directory`
     inspect an ALREADY-BUILT artifact, and `assert_clean` raises (or the
     CLI exits non-zero) if anything secret-shaped is present — independent
     of whether prevention worked. This is the "post-build inspection
     gate" required by RB-4: it still catches a leak even if a future edit
     to an exclusion list reopens the hole.

Deliberately reasonable, not maximal, about what counts as secret-shaped.
A naive `"secret" in name` substring check would flag real shipped source
in THIS repo:
  - wrapper/lib/secret_redaction.py                  (source, ships)
  - wrapper/rules/innate/01-secrets-protection.yaml   (source rule, ships)
  - wrapper/rules/innate/01-secrets-protection.json   (build artifact
    vendored by make_bundle.py's innate-rule JSON step — REQUIRED at
    runtime by the wrapper's innate-rule loader; silently excluding it
    would disarm a safety rule for every customer)
None of those are secrets; they are code/config *about* secrets. The
token/secret/credential substring check therefore only fires for a narrow
set of extensions actually used for flat credential dumps (no extension,
.txt, .ini, .cfg, .conf) — never .py, .yaml/.yml, .json, .md, .js, .ts, etc.

Explicit `.example` / `.sample` / `.template` files (e.g.
server/deploy/deploy.env.example, server/.env.dev.example — both real,
already-committed onboarding templates in this repo) are never
secret-shaped, regardless of what else matches, since they are the
conventional way to document a secret's *shape* without holding a real
value.
"""
from __future__ import annotations

import fnmatch
import os
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Iterable, Mapping


class SecretShapedArtifactError(RuntimeError):
    """Raised when a built artifact contains one or more secret-shaped paths."""


# --- Denylist ---------------------------------------------------------------

# Exact (lower-cased) basenames that are always secret-shaped.
# F-02 (advisor 2026-07-10): the original denylist missed a batch of extremely
# common credential filenames that carry no `.env`/`.pem`/`.key` shape — SSH
# private keys (id_rsa & friends), package-manager credential dotfiles
# (.npmrc/.netrc/.pypirc), and cloud key stores. Added here explicitly; the
# allowlist gate below is the durable backstop for the ones we still don't name.
_SECRET_EXACT_BASENAMES: frozenset[str] = frozenset({
    ".env", "deploy.env",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",  # SSH private keys (no ext)
    ".npmrc", ".netrc", ".pypirc",                 # package-manager token dotfiles
    "token.json",                                    # common flat token store
    ".mcp.json",                                     # M-07: Claude Code MCP
                                                     # registration — carries the
                                                     # literal Bearer token; must
                                                     # never ship in an artifact
})

# Exact credential-store paths, matched at any depth relative to the scanned
# artifact. Keep these path-specific so ordinary config.json/hosts.yml/auth.json
# files elsewhere in the bundle are not treated as credentials.
_SECRET_PATH_SUFFIXES: tuple[str, ...] = (
    ".docker/config.json",
    ".config/gh/hosts.yml",
    "composer/auth.json",
    ".kube/config",
)

# Glob patterns (fnmatch, matched against the lower-cased basename).
_SECRET_GLOB_PATTERNS: tuple[str, ...] = (
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",  # incl. id_rsa.<host> forms
    "*.p12", "*.pfx", "*.pkcs12",                       # PKCS#12 key stores
    "*.keystore", "*.jks",                              # Java key stores
    "*_rsa", "*_dsa",                                   # renamed private keys
)

# Credential-bearing JSON files (OAuth client secrets, GCP service-account
# keys, …) carry a legitimate-looking `.json` extension — permitted by the
# bundle allowlist — but ARE secrets. Matched against the .json filename STEM.
# Deliberately specific credential-file names so the shipped rules JSON
# (`01-secrets-protection.json`, stem contains "secret") is NOT flagged.
_SECRET_JSON_STEM_SUBSTRINGS: tuple[str, ...] = (
    "client_secret", "client-secret", "credentials", "credential",
    "service-account", "service_account", "serviceaccount", "gcp-key", "sa-key",
)

# Directory component names (any depth, case-insensitive) that make
# everything beneath them secret-shaped.
_SECRET_DIR_NAMES: frozenset[str] = frozenset({"secrets"})

# Suffixes that mark a file as an explicit, non-secret template/example.
#
# Cross-sweep (2026-07-21): these no longer "override every other rule". A
# suffix is a NAME, and the name was being taken as proof of the content's
# nature — `secrets/prod.env.sample`, `secrets/id_rsa.example` and
# `lib/keys.p12.template` all reported secret_shaped=False AND permitted=True,
# so a real credential shipped if someone edited a committed `*.example` in
# place (this repo already ships two) or parked a scratch credential under a
# name chosen to look inert. Two changes below:
#   * `is_secret_shaped` consults the `secrets/` directory test FIRST, so
#     nothing under a secrets/ directory can exempt itself by filename;
#   * `is_permitted_bundle_path` strips the suffix and re-runs the normal
#     checks on what remains, so the exemption has to prove itself
#     (`id_rsa.example` -> `id_rsa` -> refused; `keys.p12.template` -> `.p12`
#     -> refused).
_SAFE_TEMPLATE_SUFFIXES: tuple[str, ...] = (".example", ".sample", ".template")


def _strip_template_suffix(lname: str) -> str:
    """`lname` with one trailing template suffix removed (lower-cased input).

    Returns `lname` unchanged when it carries no template suffix, and leaves a
    bare `.example` (nothing in front of the suffix) alone rather than
    collapsing it to the empty string.
    """
    for suffix in _SAFE_TEMPLATE_SUFFIXES:
        if lname.endswith(suffix) and len(lname) > len(suffix):
            return lname[: -len(suffix)]
    return lname

# Substrings that, combined with a "flat credential dump" extension
# (below), mark a file as token-like. Matched against the filename STEM
# (basename minus extension), case-insensitively.
_TOKEN_SUBSTRINGS: tuple[str, ...] = ("token", "secret", "credential")

# Extensions where a token/secret/credential substring is trusted as a
# real signal. Deliberately narrow — see module docstring for the
# concrete false positives (.py/.yaml/.json source + build files in this
# repo) a broader set would break.
_RISKY_TOKEN_EXTENSIONS: frozenset[str] = frozenset({"", ".txt", ".ini", ".cfg", ".conf"})


def is_secret_shaped(rel_path: str) -> bool:
    """True if `rel_path` (posix or windows separators, relative to the
    artifact/source root) looks like a secret/credential file."""
    posix = rel_path.replace("\\", "/")
    parts = [p for p in posix.split("/") if p not in ("", ".")]
    if not parts:
        return False
    name = parts[-1]

    # Any "secrets/" directory component anywhere in the path.
    #
    # Cross-sweep (2026-07-21): this test now runs BEFORE the template
    # exemption below. A directory the operator named `secrets/` is a
    # statement about the CONTENT; a `.example` suffix is a statement about
    # the NAME, and the name used to win. `secrets/prod.env.sample` returned
    # False.
    for part in parts[:-1]:
        if part.lower() in _SECRET_DIR_NAMES:
            return True

    # Explicit template/example files are not secret-shaped anywhere else
    # (e.g. deploy.env.example, .env.dev.example — both real, already-committed
    # onboarding templates in this repo). The bundle allowlist is the gate that
    # still asks what the stripped name actually is.
    if name.endswith(_SAFE_TEMPLATE_SUFFIXES):
        return False

    lname = name.lower()
    if lname in _SECRET_EXACT_BASENAMES:
        return True
    lposix = "/".join(part.lower() for part in parts)
    if any(lposix == suffix or lposix.endswith(f"/{suffix}")
           for suffix in _SECRET_PATH_SUFFIXES):
        return True
    for pattern in _SECRET_GLOB_PATTERNS:
        if fnmatch.fnmatchcase(lname, pattern):
            return True

    stem, ext = os.path.splitext(lname)
    if ext in _RISKY_TOKEN_EXTENSIONS:
        for sub in _TOKEN_SUBSTRINGS:
            if sub in stem:
                return True

    # Credential-bearing JSON (F-02): .json is a permitted bundle extension, so a
    # credential JSON slips past both the extension checks AND the allowlist gate.
    # Name-check it here.
    if ext == ".json":
        for sub in _SECRET_JSON_STEM_SUBSTRINGS:
            if sub in stem:
                return True

    return False


# --- Bundle allowlist (advisor Root B) --------------------------------------

# Extensions permitted to ship in the CUSTOMER bundle. This is the durable
# answer to "a denylist always misses the next secret filename" (advisor Root B,
# 2026-07-10): anything whose type is not on this allowlist is refused, so an
# unknown-shaped credential file (id_rsa, foo.p12, weird.secretstore, …) cannot
# ship even if the denylist never learns its name. Derived from the actual set of
# file types the wrapper bundle ships (py/md/yaml/json/txt/sh/ps1/toml/…).
#
# Bundle-specific ON PURPOSE — it is NOT folded into is_secret_shaped(), because
# is_secret_shaped() is shared with the SERVER deploy tarball, whose permitted
# content differs. Only make_bundle.py's customer-bundle gate calls this.
_PERMITTED_BUNDLE_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".pyi", ".md", ".yaml", ".yml", ".json", ".txt", ".sh", ".ps1", ".toml",
})

# Permitted extensionless / dotfile basenames (lower-cased).
_PERMITTED_BUNDLE_BASENAMES: frozenset[str] = frozenset({
    ".gitignore", "license", "readme", "notice", "manifest",
})


def is_permitted_bundle_path(rel_path: str) -> bool:
    """True if `rel_path` is a file TYPE permitted to ship in the customer
    bundle (allowlist). A small set of known extensionless basenames is
    permitted; everything else must carry a permitted extension.

    Cross-sweep (2026-07-21) — templates no longer short-circuit this. A
    trailing `.example`/`.sample`/`.template` used to return True BEFORE the
    extension allowlist was consulted, which meant the allowlist that exists
    precisely because "a denylist always misses the next secret filename"
    (advisor Root B) could be bypassed with a filename suffix:
    `secrets/id_rsa.example`, `lib/keys.p12.template` and
    `secrets/customer_db_dump.sqlite.example` were all permitted to ship.

    The exemption now has to prove itself: strip one template suffix and ask
    the normal question of what remains. `foo.yaml.example` -> `foo.yaml` ->
    permitted. `id_rsa.example` -> `id_rsa` -> not on the allowlist, refused.
    A remainder that is itself secret-shaped (`deploy.env.example` ->
    `deploy.env`) is refused too — the allowlist is the durable backstop, and
    a credential template is not a type the CUSTOMER bundle needs to carry.
    """
    posix = rel_path.replace("\\", "/")
    parts = [p for p in posix.split("/") if p not in ("", ".")]
    if not parts:
        return False
    lname = parts[-1].lower()
    stripped = _strip_template_suffix(lname)
    if stripped != lname:
        # Judge the template by what it is a template OF, in its real location.
        if is_secret_shaped("/".join(parts[:-1] + [stripped])):
            return False
        lname = stripped
    if lname in _PERMITTED_BUNDLE_BASENAMES:
        return True
    _, ext = os.path.splitext(lname)
    return ext in _PERMITTED_BUNDLE_EXTENSIONS


def find_disallowed_bundle_paths(paths: Iterable[str]) -> list[str]:
    """Return the subset of `paths` NOT permitted to ship in the customer
    bundle, in input order (directory entries — trailing '/' — are ignored)."""
    return [
        p for p in paths
        if not p.replace("\\", "/").endswith("/") and not is_permitted_bundle_path(p)
    ]


def find_secret_shaped(paths: Iterable[str]) -> list[str]:
    """Return the secret-shaped subset of `paths`, in input order, with
    separators NORMALIZED to `/`.

    Finding 4 (2026-07-26): matching already normalized internally
    (`is_secret_shaped` folds `\\` to `/`), but the REPORT echoed the raw input,
    so a member literally named `foo\\.kube\\config` was reported verbatim. The
    ZIP format specifies `/` as its separator, so such a name is a single
    filename containing backslashes — and the surrounding tooling, plus the
    operator reading the failure, expect one canonical spelling. Returning the
    raw form made `test_run01_secret_scan_catches_common_credential_store_filenames`
    pass on Windows and fail on Linux for the same archive: a scanner whose
    output depends on the host it runs on cannot be compared across CI legs.

    Detection is unchanged — only the reported spelling is canonicalized.
    """
    return [p.replace("\\", "/") for p in paths if is_secret_shaped(p)]


# --- Artifact scanners -------------------------------------------------------

def scan_zip(zip_path: str | Path) -> list[str]:
    """Return the secret-shaped entry names inside a .zip archive."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    return find_secret_shaped(names)


def scan_tar(tar_path: str | Path) -> list[str]:
    """Return the secret-shaped member names inside a tar archive (any
    compression — mode "r:*" auto-detects)."""
    with tarfile.open(tar_path, mode="r:*") as tf:
        names = [m.name for m in tf.getmembers()]
    return find_secret_shaped(names)


def _validated_trusted_exact_files(
    trusted_exact_files: Mapping[str, bytes] | None,
) -> dict[str, bytes]:
    """Return a canonical copy of caller-held exact generated-file bytes.

    This is deliberately an *external attestation*, not an artifact manifest:
    the expected bytes must come from trusted source code or another authority
    outside the artifact being inspected.  The ordinary scanner passes no
    mapping and remains fully fail-closed for every secret-shaped path.
    """
    validated: dict[str, bytes] = {}
    for rel, expected in (trusted_exact_files or {}).items():
        if not isinstance(rel, str) or not rel:
            raise TypeError("trusted exact file paths must be non-empty strings")
        canonical = rel.replace("\\", "/")
        parts = canonical.split("/")
        if (
            canonical != rel
            or canonical.startswith("/")
            or ":" in parts[0]
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError(f"trusted exact file path must be canonical and relative: {rel!r}")
        if not isinstance(expected, bytes):
            raise TypeError(f"trusted exact bytes for {rel!r} must be bytes")
        validated[canonical] = expected
    return validated


def scan_directory(
    root: str | Path,
    *,
    trusted_exact_files: Mapping[str, bytes] | None = None,
) -> list[str]:
    """Return the secret-shaped relative file paths under `root`.

    `trusted_exact_files` is a narrow escape hatch for source-generated files
    whose *name* is conservatively secret-shaped but whose exact bytes are
    known by the trusted builder.  It is path-bound, byte-exact, and never
    populated from an artifact's own manifest.  Symlinks cannot qualify.
    """
    root = Path(root)
    exact = _validated_trusted_exact_files(trusted_exact_files)
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            if is_secret_shaped(rel):
                expected = exact.get(rel)
                if expected is not None and not path.is_symlink() and path.read_bytes() == expected:
                    continue
                hits.append(rel)
    return hits


def scan_artifact(path: str | Path) -> list[str]:
    """Dispatch to the right scanner based on what `path` is."""
    path = Path(path)
    if path.is_dir():
        return scan_directory(path)
    if zipfile.is_zipfile(path):
        return scan_zip(path)
    if tarfile.is_tarfile(path):
        return scan_tar(path)
    raise ValueError(f"unrecognized artifact type (not a dir/zip/tar): {path}")


def assert_clean(hits: list[str], artifact_label: str) -> None:
    """Raise SecretShapedArtifactError if `hits` is non-empty."""
    if hits:
        lines = "\n".join(f"  - {h}" for h in hits)
        raise SecretShapedArtifactError(
            f"REFUSED: {artifact_label} contains {len(hits)} secret-shaped "
            f"path(s):\n{lines}\n"
            "These look like credentials/secrets and must never ship. Remove "
            "them from the source tree (or close the exclusion-list gap that "
            "let them through) before shipping."
        )


# --- CLI (used by deploy_staging.sh's post-build gate) -----------------------

def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Fail (non-zero exit) if a built artifact (zip/tar/dir) "
        "contains secret-shaped paths."
    )
    parser.add_argument("artifact", help="Path to a .zip, .tar[.gz], or a directory.")
    args = parser.parse_args(argv)

    target = Path(args.artifact)
    if not target.exists():
        print(f"ERROR: artifact not found: {target}", file=sys.stderr)
        return 1

    try:
        hits = scan_artifact(target)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        assert_clean(hits, str(target))
    except SecretShapedArtifactError as e:
        print(str(e), file=sys.stderr)
        return 1

    print(f"OK: {target} — no secret-shaped paths found")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
