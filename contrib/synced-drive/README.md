# Synced-drive placeholders

`materialize.py` fixes jobs that fail on cloud-only synced-drive files with `OSError: [Errno 11] Resource deadlock avoided` (EDEADLK).

Processes started by launchd or over ssh can inherit an I/O policy that forbids downloading dataless files. Finder and Terminal do not, which is why the same read works by hand and fails on a schedule.

Call it once, before the job touches the synced folder:

```python
from materialize import allow_materialize
allow_materialize()
```

It sets the process policy with `setiopolicy_np(IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS, ON)`. The first read of a placeholder then waits for the download (a few seconds) instead of failing. Off macOS it does nothing.

Use `is_placeholder_error(exc)` to tell this case apart from other read errors in logs.
