# Backup and restore (Docker Compose)

The only data worth protecting is the **PostgreSQL** database (jobs, audit log, LLM usage, reviewer feedback). Downloaded Ollama models
can be re-pulled, and Prometheus/Grafana data is disposable.

## Back up
```bash
scripts/backup.sh                         # -> backups/devopsagents-<UTC timestamp>.dump
make backup                               # same thing
KEEP=30 BACKUP_DIR=/mnt/usb scripts/backup.sh
```
- Uses `pg_dump --format=custom` (compressed, restorable with `pg_restore`). Dumps are written to a `.partial` file and renamed only if
  `pg_dump` succeeded **and** the file is non-empty, so a failed run never leaves something that looks like a good backup.
- Keeps the newest `KEEP` dumps (default 14) and deletes older ones. Ordering uses the timestamp in the file name, not file times.
- Files are created owner-only (`umask 077`): they contain your data (job payloads, audit entries). `backups/` is git-ignored.

### Schedule it
```bash
# crontab -e   (daily at 02:30; use the absolute repo path)
30 2 * * * cd /path/to/ai-devops-agents && scripts/backup.sh >> backups/backup.log 2>&1
```
Copy the dumps somewhere other than the same disk (external drive, another machine). A backup on the disk that fails is not a backup.

## Restore
```bash
scripts/restore.sh backups/devopsagents-20261117T023000Z.dump        # asks you to type "restore"
make restore FILE=backups/devopsagents-20261117T023000Z.dump
FORCE=1 scripts/restore.sh <file>                                    # no prompt (automation)
```
It **replaces the current database**. The script stops `api` and `worker`, runs `pg_restore --clean --if-exists --no-owner`, then
starts them again. If the restore fails it tells you the services are still stopped.

After restoring, run `docker compose up -d` (applies any newer migrations) and check `curl http://127.0.0.1:8000/health/ready`.

## Test your backups
An untested backup is a hope. Once in a while restore into a scratch stack:
```bash
docker compose -p restore-test up -d postgres
# then restore a dump into that project's postgres and inspect it, then:  docker compose -p restore-test down -v
```

## Windows
The scripts are bash: run them from **Git Bash** or **WSL** (`.gitattributes` keeps them LF so they work after a Windows checkout).
From plain PowerShell the equivalent commands are:
```powershell
docker compose exec -T postgres pg_dump -U devopsagents -d devopsagents --format=custom > backups\manual.dump
Get-Content -AsByteStream backups\manual.dump | docker compose exec -T postgres pg_restore -U devopsagents -d devopsagents --clean --if-exists --no-owner
```
(PowerShell 5.1 corrupts binary redirection; use PowerShell 7 or Git Bash for dumps.)

## Limits
- The scripts are tested against a fake `docker` command (ordering, retention, failure handling, confirmation), not a real database;
  run a real backup and restore once on your machine before relying on them.
- No encryption of dumps and no off-machine upload: add those for sensitive data (e.g. `age`/`gpg`, `rclone`).
- Metrics and Grafana state are not included.
