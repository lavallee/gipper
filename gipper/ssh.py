"""ssh to a rented machine with gipper's own key; files move as tar streams over the same
connection (no rsync or Runpod CLI needed on either side)."""
import shlex
import subprocess
import time
from pathlib import Path

from gipper.config import KEY_DIR

KEY = KEY_DIR / "id_ed25519"


def ensure_key():
    if not KEY.exists():
        KEY.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "gipper", "-f", str(KEY)], check=True)
    return KEY.with_suffix(".pub").read_text().strip()


class Remote:
    def __init__(self, host, port, user="root"):
        self.host, self.port, self.user = host, int(port), user

    @property
    def home(self):
        """Containers log in as root; VMs (Shadeform) as an ordinary user."""
        return "/root" if self.user == "root" else f"/home/{self.user}"

    def _base(self):
        return ["ssh", "-i", str(KEY), "-p", str(self.port), "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=30",
                "-o", "ConnectTimeout=15", f"{self.user}@{self.host}"]

    def run(self, cmd, check=True, capture=True, timeout=None):
        r = subprocess.run(self._base() + [cmd], capture_output=capture, text=True, timeout=timeout)
        if check and r.returncode:
            raise RuntimeError(f"ssh `{cmd[:80]}` exited {r.returncode}: {(r.stderr or '')[-400:]}")
        return r

    def wait(self, timeout_s=600):
        t0 = time.time()
        while time.time() - t0 < timeout_s:
            try:
                if self.run("true", check=False, timeout=30).returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(5)
        return False

    def put(self, src, dst, exclude=()):
        """Local file or directory -> remote path (created)."""
        src = Path(src)
        ex = [f"--exclude={e}" for e in exclude]
        if src.is_dir():
            tar = ["tar", "czf", "-", *ex, "-C", str(src), "."]
            remote = f"mkdir -p {shlex.quote(dst)} && tar xzf - -C {shlex.quote(dst)}"
        else:
            tar = ["tar", "czf", "-", "-C", str(src.parent), src.name]
            parent = str(Path(dst).parent)
            remote = (f"mkdir -p {shlex.quote(parent)} && tar xzf - -C {shlex.quote(parent)}"
                      f" && [ {shlex.quote(src.name)} = {shlex.quote(Path(dst).name)} ] || mv {shlex.quote(parent + '/' + src.name)} {shlex.quote(dst)}")
        p1 = subprocess.Popen(tar, stdout=subprocess.PIPE)
        p2 = subprocess.run(self._base() + [remote], stdin=p1.stdout, capture_output=True, text=True)
        p1.stdout.close()
        if p1.wait() or p2.returncode:
            raise RuntimeError(f"upload {src} -> {dst} failed: {p2.stderr[-400:]}")

    def get(self, src, dst):
        """Remote directory (or file) -> local directory dst (merged, overwriting)."""
        Path(dst).mkdir(parents=True, exist_ok=True)
        if self.run(f"test -e {shlex.quote(src)}", check=False, timeout=60).returncode:
            return False  # nothing there (yet): not an error
        remote = (f"if [ -d {shlex.quote(src)} ]; then tar czf - -C {shlex.quote(src)} .; "
                  f"elif [ -e {shlex.quote(src)} ]; then tar czf - -C $(dirname {shlex.quote(src)}) $(basename {shlex.quote(src)}); fi")
        p1 = subprocess.Popen(self._base() + [remote], stdout=subprocess.PIPE)
        p2 = subprocess.run(["tar", "xzf", "-", "-C", str(dst)], stdin=p1.stdout, capture_output=True, text=True)
        p1.stdout.close()
        p1.wait()
        if p2.returncode and "empty" not in p2.stderr:
            raise RuntimeError(f"download {src} -> {dst} failed: {p2.stderr[-400:]}")
        return True
