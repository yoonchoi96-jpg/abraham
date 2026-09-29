# Abraham V5.6 production deployment

V5.6 adds a storage lifecycle layer without automatically deleting or moving Obsidian Vault notes.

## Safe policy
- SQLite remains the source of truth.
- Obsidian Vault is never automatically archived/deleted by maintenance.
- Old backup files are compressed.
- Old backup snapshots beyond retention are removed.
- Old reports and logs are pruned.
- Disk usage is monitored with warning/critical thresholds.
- Long-term archive can be configured separately with `ABRAHAM_ARCHIVE_PATH`.

## Files
- `run_v560_storage.py`: Abraham V5.6 pipeline
- `abraham_storage.py`: storage status and safe maintenance
- `abraham_incremental.py`: stage state
- `abraham_sqlite.py`: SQLite repository
- `abraham-storage.service/.timer`: daily safe maintenance
- `abraham-backup.service/.timer`: SQLite backup
- `abraham-headless-sync.service`: Obsidian continuous sync
- `abraham.service/.timer`: three daily pipeline runs (00:00 / 09:00 / 17:00 KST)

## Useful commands
```bash
sudo -u abraham /opt/abraham/venv/bin/python /opt/abraham/app/run_v560_storage.py --storage-status
sudo -u abraham /opt/abraham/venv/bin/python /opt/abraham/app/run_v560_storage.py --storage-maintenance
```
The second command is dry-run unless `--apply` is supplied.

## Important
Do not automatically move old Markdown notes out of the synced Vault. That should only be introduced later as an explicit archive workflow after link/sync semantics are tested.
