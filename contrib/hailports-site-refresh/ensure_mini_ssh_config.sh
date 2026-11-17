#!/usr/bin/env bash
# Keep Host mini in ~/.ssh/config pointed at the SOCKS hop from nightly.local.
# Never prints the user or host. Safe to run from cron.
set -euo pipefail
CFG=${STACKMINI_NIGHTLY_LOCAL:-$HOME/claude-stack/data/runtime/stackmini_nightly.local}
HELPER=${STACKMINI_SOCKS:-$HOME/claude-stack/bin/socks5-connect.py}
SSH_CONFIG=${HP_SSH_CONFIG:-$HOME/.ssh/config}
IDENTITY=${HP_SSH_IDENTITY:-$HOME/.ssh/id_ed25519}
USER=; HOST=
if [ ! -f "$CFG" ]; then
  echo "ensure_mini_ssh_config: nightly peer file missing" >&2
  exit 2
fi
if [ ! -f "$HELPER" ]; then
  echo "ensure_mini_ssh_config: socks helper missing" >&2
  exit 2
fi
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in ""|\#*) continue ;; esac
  key=${line%%=*}
  val=${line#*=}
  case "$key" in
    user) USER=$val ;;
    host) HOST=$val ;;
  esac
done < "$CFG"
if [ -z "$USER" ] || [ -z "$HOST" ]; then
  echo "ensure_mini_ssh_config: user/host missing" >&2
  exit 2
fi
mkdir -p "$(dirname "$SSH_CONFIG")"
chmod 700 "$(dirname "$SSH_CONFIG")" 2>/dev/null || true
block=$(printf '%s\n' \
  '# BEGIN HAILPORTS-MINI' \
  'Host mini' \
  "  HostName $HOST" \
  "  User $USER" \
  "  IdentityFile $IDENTITY" \
  '  IdentitiesOnly yes' \
  '  BatchMode yes' \
  '  ConnectTimeout 20' \
  '  StrictHostKeyChecking yes' \
  "  ProxyCommand python3 $HELPER %h %p" \
  '# END HAILPORTS-MINI')
python3 - "$SSH_CONFIG" "$block" << 'PY'
import os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
block = sys.argv[2].rstrip() + "\n"
text = path.read_text() if path.exists() else ""
start, end = "# BEGIN HAILPORTS-MINI", "# END HAILPORTS-MINI"
if start in text and end in text:
    pre = text[:text.index(start)]
    post = text[text.index(end) + len(end):].lstrip("\n")
    new = pre.rstrip("\n") + ("\n\n" if pre.strip() else "") + block + ("\n" + post if post.strip() else "")
else:
    new = (text.rstrip() + "\n\n" if text.strip() else "") + block
if new != text:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent if path.parent.exists() else "/tmp"), prefix=".sshconfig.")
    try:
        os.write(fd, new.encode())
        os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    print("ensure_mini_ssh_config: wrote Host mini")
else:
    print("ensure_mini_ssh_config: Host mini already current")
PY
chmod 600 "$SSH_CONFIG"
