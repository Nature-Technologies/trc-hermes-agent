"""Launch one sandbox run: limits, then drop to the slot's uid, then exec the script.

A separate process on purpose: the runner is threaded, and `preexec_fn` is unsafe in a
threaded parent (trc-backend spec §5.2). argv: uid cpu_seconds mem_mb workdir rpc_fd.
`-E -s` ignore the environment and user site-packages; `-I` is not used because it also
drops the script's own directory from sys.path, where hermes_tools.py sits.
"""

import os
import resource
import sys

NPROC = 32
FSIZE = 50 * 2**20
NOFILE = 64


def main(argv):
    uid, cpu, mem_mb, workdir, rpc_fd = int(argv[0]), int(argv[1]), int(argv[2]), argv[3], argv[4]
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
    resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE, FSIZE))
    resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE, NOFILE))
    os.setgroups([])
    os.setgid(uid)
    os.setuid(uid)
    os.chdir(workdir)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": workdir,
        "TMPDIR": workdir,
        "LANG": "C.UTF-8",
        "TZ": "UTC",
        "HERMES_RPC_FD": rpc_fd,
        # Numeric libraries' thread pools count against RLIMIT_NPROC.
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    os.execve(
        sys.executable,
        [sys.executable, "-E", "-s", "-B", "-X", "utf8", "script.py"],
        env,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
