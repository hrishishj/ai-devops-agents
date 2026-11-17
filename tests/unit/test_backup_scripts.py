"""Run the real backup/restore scripts against a fake `docker` command (no Docker needed)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

SHIM = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *pg_dump*)
    if [ "${FAKE_DUMP_FAIL:-}" = "1" ]; then echo "pg_dump: boom" >&2; exit 3; fi
    printf '%s' "${FAKE_DUMP_CONTENT-PGDMP-fake-dump}"
    ;;
  *pg_restore*)
    cat > "$FAKE_RESTORE_INPUT"
    if [ "${FAKE_RESTORE_FAIL:-}" = "1" ]; then echo "pg_restore: boom" >&2; exit 4; fi
    ;;
esac
"""


@pytest.fixture
def env(tmp_path, repo_root):
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    (shim_dir / "docker").write_text(SHIM, encoding="utf-8", newline="\n")
    (shim_dir / "docker").chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    e = dict(os.environ)
    e["PATH"] = f"{shim_dir.as_posix()}{os.pathsep}{e['PATH']}"
    e["FAKE_DOCKER_LOG"] = (tmp_path / "docker.log").as_posix()
    e["FAKE_RESTORE_INPUT"] = (tmp_path / "restore.in").as_posix()
    e["BACKUP_DIR"] = "backups"
    return {
        "env": e,
        "cwd": work,
        "log": tmp_path / "docker.log",
        "restore_in": tmp_path / "restore.in",
        "scripts": repo_root / "scripts",
    }


def run(rig, script, *args, stdin=None, **extra_env):
    e = {**rig["env"], **extra_env}
    return subprocess.run(
        [BASH, (rig["scripts"] / script).as_posix(), *args],
        cwd=rig["cwd"],
        env=e,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
    )


def dumps(rig):
    d = rig["cwd"] / "backups"
    return sorted(p.name for p in d.glob("*")) if d.exists() else []


def log_lines(rig):
    return rig["log"].read_text(encoding="utf-8").splitlines() if rig["log"].exists() else []


def test_scripts_have_valid_bash_syntax(repo_root):
    for name in ("backup.sh", "restore.sh"):
        r = subprocess.run(
            [BASH, "-n", (repo_root / "scripts" / name).as_posix()], capture_output=True, text=True
        )
        assert r.returncode == 0, r.stderr


# ---- backup ---------------------------------------------------------------------------------------------
def test_backup_writes_a_timestamped_dump_and_prints_its_path(env):
    r = run(env, "backup.sh")
    assert r.returncode == 0, r.stderr
    names = dumps(env)
    assert len(names) == 1 and re.fullmatch(r"devopsagents-\d{8}T\d{6}Z\.dump", names[0])
    assert r.stdout.strip().endswith(names[0])
    assert (env["cwd"] / "backups" / names[0]).read_text() == "PGDMP-fake-dump"
    cmd = log_lines(env)[0]
    assert "compose exec -T postgres pg_dump -U devopsagents -d devopsagents --format=custom" in cmd


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_backups_are_owner_only(env):
    run(env, "backup.sh")
    f = env["cwd"] / "backups" / dumps(env)[0]
    assert (f.stat().st_mode & 0o077) == 0


def test_a_failed_pg_dump_leaves_no_file_and_keeps_old_backups(env):
    old = env["cwd"] / "backups"
    old.mkdir()
    (old / "devopsagents-20200101T000000Z.dump").write_text("old")
    r = run(env, "backup.sh", FAKE_DUMP_FAIL="1")
    assert r.returncode != 0
    assert dumps(env) == ["devopsagents-20200101T000000Z.dump"]  # no partial, nothing pruned


def test_an_empty_dump_is_rejected(env):
    r = run(env, "backup.sh", FAKE_DUMP_CONTENT="")
    assert r.returncode == 1 and "empty" in r.stderr and dumps(env) == []


def test_retention_keeps_only_the_newest_n(env):
    d = env["cwd"] / "backups"
    d.mkdir()
    for i in range(1, 6):  # five old dumps, oldest first
        f = d / f"devopsagents-2020010{i}T000000Z.dump"
        f.write_text("x")
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    run(env, "backup.sh", KEEP="3")
    names = dumps(env)
    assert len(names) == 3
    assert (
        "devopsagents-20200101T000000Z.dump" not in names
        and "devopsagents-20200102T000000Z.dump" not in names
    )
    assert "devopsagents-20200105T000000Z.dump" in names  # newest old one survives alongside the new dump


@pytest.mark.parametrize("keep", ["0", "abc", "-1", "2x"])
def test_invalid_retention_is_rejected_before_anything_runs(env, keep):
    r = run(env, "backup.sh", KEEP=keep)
    assert r.returncode == 2 and log_lines(env) == []


# ---- restore ----------------------------------------------------------------------------------------------
def make_dump(rig, content="PGDMP-restore-me"):
    f = rig["cwd"] / "good.dump"
    f.write_text(content, encoding="utf-8", newline="")
    return f.name


def test_restore_validates_its_input(env):
    assert run(env, "restore.sh").returncode == 2
    assert "no such file" in run(env, "restore.sh", "missing.dump").stderr
    (env["cwd"] / "empty.dump").write_text("")
    assert "empty" in run(env, "restore.sh", "empty.dump").stderr
    assert log_lines(env) == []  # nothing was touched


def test_restore_requires_explicit_confirmation(env):
    name = make_dump(env)
    declined = run(env, "restore.sh", name, stdin="no\n")
    assert declined.returncode == 1 and "aborted" in declined.stderr and log_lines(env) == []
    ok = run(env, "restore.sh", name, stdin="restore\n")
    assert ok.returncode == 0 and "restored from" in ok.stdout


def test_restore_stops_services_restores_then_starts_them_in_order(env):
    name = make_dump(env)
    r = run(env, "restore.sh", name, FORCE="1")
    assert r.returncode == 0, r.stderr
    lines = log_lines(env)
    assert lines[0].endswith("compose stop api worker")
    assert "pg_restore -U devopsagents -d devopsagents --clean --if-exists --no-owner" in lines[1]
    assert lines[2].endswith("compose up -d api worker")
    assert env["restore_in"].read_text() == "PGDMP-restore-me"  # the dump really reached pg_restore


def test_a_failed_restore_says_the_services_remain_stopped(env):
    name = make_dump(env)
    r = run(env, "restore.sh", name, FORCE="1", FAKE_RESTORE_FAIL="1")
    assert r.returncode != 0 and "remain stopped" in r.stderr
    assert not any(line.endswith("up -d api worker") for line in log_lines(env))  # never restarted on failure
