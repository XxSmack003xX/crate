"""Container entrypoint (like MEDIARR's docker-entrypoint.sh, in Python).

Started as root so it can give Crate access to the Docker socket (needed for
one-click updates): the socket on Linux is usually root:docker 0660 and the host's
docker group ID isn't known in advance, so we add the socket's actual group, make
/data writable, then drop to PUID:PGID before starting Crate.
If the container was started as a non-root user (an older compose file with
`user:`), it just runs the command.
"""
import os
import sys


def main():
    cmd = sys.argv[1:] or ["python", "-m", "crate", "--port", "9325"]
    if os.getuid() == 0 and os.environ.get("CRATE_RUN_AS_ROOT") != "1":
        uid = int(os.environ.get("PUID") or 1000)
        gid = int(os.environ.get("PGID") or 1000)
        groups = {gid}
        sock = os.environ.get("CRATE_DOCKER_SOCKET", "/var/run/docker.sock")
        try:
            if os.path.exists(sock):
                groups.add(os.stat(sock).st_gid)
        except OSError:
            pass
        data = os.path.dirname(os.environ.get("CRATE_DB", "/data/crate.db")) or "/data"
        try:
            for root, dirs, files in os.walk(data):
                for n in [root] + [os.path.join(root, f) for f in files + dirs]:
                    st = os.lstat(n)
                    if st.st_uid != uid:
                        os.lchown(n, uid, gid)
        except OSError as e:
            print(f"[entrypoint] couldn't fix ownership of {data}: {e}", flush=True)
        os.setgroups(sorted(groups))
        os.setgid(gid)
        os.setuid(uid)
        os.environ.setdefault("HOME", "/tmp")
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
