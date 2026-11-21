from __future__ import annotations

import json
import os
import selectors
import subprocess
import time

from evocoder.contracts import Settings, safe_path


def bounded_process(argv: list[str], timeout: float, limit=32000) -> dict:
    """Never execute repository commands on the host. This helper invokes Docker only."""
    started = time.monotonic()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    output = bytearray()
    timed_out = False
    try:
        while selector.get_map():
            if time.monotonic() - started > timeout:
                timed_out = True
                break
            for key, _ in selector.select(0.05):
                data = os.read(key.fd, 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    break
                output.extend(data[:max(0, limit - len(output))])
        if timed_out:
            proc.kill()
        try:
            code = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()
    finally:
        selector.close()
        proc.stdout.close()
    return {"exit_code": code, "output": output.decode(errors="replace"),
            "timeout": timed_out, "output_limit": len(output) >= limit,
            "seconds": round(time.monotonic() - started, 4)}


class Sandbox:
    """Disposable, networkless Docker container. Only a fresh task copy is mounted."""

    def __init__(self, files: dict[str, str], settings: Settings, label: str):
        self.files = files
        self.settings = settings
        self.label = label
        self.container = None
        self.image_id = None

    def __enter__(self):
        for name in self.files:
            safe_path(name)
        if sum(len(v.encode()) for v in self.files.values()) > 2_000_000:
            raise ValueError("sandbox input exceeds 2MB")
        inspected = bounded_process(["docker", "image", "inspect", self.settings.image,
                                     "--format", "{{.Id}}"], 10)
        if inspected["exit_code"] != 0 or inspected["timeout"]:
            raise RuntimeError("sandbox image missing; prepare the task image before running")
        self.image_id = inspected["output"].strip()
        args = ["docker", "run", "--detach", "--rm", "--pull=never", "--network=none",
                "--read-only", "--user=65534:65534", "--cap-drop=ALL", "--pids-limit=64",
                "--memory=256m", "--memory-swap=256m", "--cpus=1", "--security-opt=no-new-privileges",
                "--tmpfs=/tmp:rw,nosuid,nodev,size=32m",
                "--tmpfs=/workspace:rw,nosuid,nodev,size=8m,mode=1777", "--env=PYTHONDONTWRITEBYTECODE=1",
                "--env=PYTHONUNBUFFERED=1", "--label=evocoder.managed=true",
                f"--label=evocoder.run={self.label}", "--workdir=/workspace",
                self.image_id, "python", "-I", "-c", "import time; time.sleep(3600)"]
        try:
            result = bounded_process(args, 20)
            if result["timeout"] or result["exit_code"] != 0:
                raise RuntimeError("sandbox_start_failed: " + result["output"][-1500:])
            self.container = result["output"].strip()
            seed_code = ("import json,pathlib,sys; files=json.load(sys.stdin); "
                         "[( (pathlib.Path('/workspace')/k).parent.mkdir(parents=True,exist_ok=True), "
                         "(pathlib.Path('/workspace')/k).write_text(v)) for k,v in files.items()]")
            seeded = subprocess.run(["docker", "exec", "-i", self.container, "python", "-I", "-c", seed_code],
                                    input=json.dumps(self.files), capture_output=True, text=True, timeout=10)
            if seeded.returncode != 0:
                raise RuntimeError("sandbox seed failed: " + seeded.stderr[-1500:])
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def execute(self, argv: list[str]) -> dict:
        if not self.container:
            raise RuntimeError("sandbox closed")
        if not argv or any("\x00" in a or len(a) > 160000 for a in argv):
            raise ValueError("invalid command argv")
        result = bounded_process(["docker", "exec", self.container, *argv], self.settings.command_timeout)
        if result["timeout"]:
            # Killing the docker client alone does not kill the process inside the container.
            self.close()
            raise RuntimeError("command_timeout: sandbox destroyed")
        return result

    def read(self, path: str, start_line=1, line_count=120) -> dict:
        path = safe_path(path)
        if start_line < 1 or not 1 <= line_count <= 200:
            raise ValueError("invalid read window")
        code = ("import pathlib; p=pathlib.Path('/workspace') / " + repr(path)
                + "; r=p.resolve(); assert r.is_relative_to('/workspace'); "
                "assert p.stat().st_size <= 500000; lines=p.read_text().splitlines(); "
                + f"print('Lines {start_line}-{min(start_line + line_count - 1, 999999999)} /',len(lines)); "
                + "print('\\n'.join(f'{i+1}: {lines[i]}' for i in "
                + f"range({start_line - 1},min({start_line - 1 + line_count},len(lines)))))")
        return self.execute(["python", "-I", "-c", code])

    def edit(self, path: str, old_text: str, new_text: str) -> dict:
        path = safe_path(path)
        if not old_text or old_text == new_text:
            raise ValueError("edit must replace nonempty text with different text")
        code = ("import pathlib; p=pathlib.Path('/workspace') / " + repr(path)
                + "; assert p.resolve().is_relative_to('/workspace'); assert not p.is_symlink(); "
                "assert p.stat().st_size <= 500000; text=p.read_text(); old=" + repr(old_text)
                + "; new=" + repr(new_text)
                + "; assert text.count(old)==1, 'old_text must occur exactly once; file unchanged'; "
                "updated=text.replace(old,new,1); assert len(updated.encode()) <= 500000; "
                "p.write_text(updated); print('edited one exact match')")
        return self.execute(["python", "-I", "-c", code])

    def write(self, path: str, content: str) -> dict:
        path = safe_path(path)
        code = ("import pathlib; p=pathlib.Path('/workspace') / " + repr(path)
                + "; assert p.resolve().is_relative_to('/workspace'); p.write_text(" + repr(content)
                + "); print('written')")
        return self.execute(["python", "-I", "-c", code])

    def search(self, query: str, path: str = "") -> dict:
        path = safe_path(path) if path else ""
        code = """import pathlib
q = QUERY
root = pathlib.Path('/workspace')
scope = root / SCOPE
assert scope.resolve().is_relative_to(root)
paths = [scope] if scope.is_file() else sorted(scope.rglob('*.py'))
matches = 0
for p in paths:
    if not p.resolve().is_relative_to(root) or p.is_symlink() or p.stat().st_size > 500000:
        continue
    for i, line in enumerate(p.read_text().splitlines(), 1):
        if q in line:
            print(f'{p.relative_to("/workspace")}:{i}:{line}')
            matches += 1
            if matches >= 80:
                print('[search truncated at 80 matches; narrow the path or query]')
                raise SystemExit(0)
if not matches:
    print('[no matches]')
""".replace("SCOPE", repr(path), 1).replace("QUERY", repr(query), 1)
        return self.execute(["python", "-I", "-c", code])

    def export(self, editable: list[str]) -> dict[str, str]:
        # Read inside the mount namespace; docker cp omits tmpfs mount contents.
        # -I isolates Python from repository imports. No source is executed by this exporter.
        code = """import json,pathlib
paths = PATHS
root = pathlib.Path('/workspace')
result = {}
for name in paths:
    p = root / name
    assert p.resolve().is_relative_to(root), 'export escapes workspace'
    assert not any(x.is_symlink() for x in [p, *p.parents]), 'symlink export forbidden'
    assert p.is_file() and p.stat().st_size <= 500000, 'invalid source export'
    result[name] = p.read_bytes().decode("utf-8")
print(json.dumps(result))
""".replace("PATHS", repr(editable), 1)
        result = bounded_process(["docker", "exec", self.container, "python", "-I", "-c", code],
                                 10, limit=2_000_000)
        if result["timeout"] or result["output_limit"] or result["exit_code"] != 0:
            raise ValueError("invalid or incomplete source export")
        found = json.loads(result["output"])
        if set(found) != set(editable):
            raise ValueError("missing editable source file")
        return found

    def close(self):
        if self.container:
            bounded_process(["docker", "rm", "--force", self.container], 10)
            self.container = None

    def __exit__(self, *args):
        self.close()


def doctor(settings: Settings) -> dict:
    result = bounded_process(["docker", "info", "--format", "{{json .ServerVersion}}"], 10)
    image = bounded_process(["docker", "image", "inspect", settings.image, "--format", "{{.Id}}"], 10)
    return {"docker_ready": result["exit_code"] == 0, "image_ready": image["exit_code"] == 0,
            "image": settings.image, "server": result["output"].strip(),
            "mode": settings.mode, "api_key_configured": bool(settings.api_key),
            "state_dir": str(settings.state_dir.resolve()), "python": "3.12"}
