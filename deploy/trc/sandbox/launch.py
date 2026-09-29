"""Launch one sandbox run: limits, then drop to the slot's uid, then exec the script.

A separate process on purpose: the runner is threaded, and `preexec_fn` is unsafe in a
threaded parent (trc-backend spec §5.2). argv: uid cpu_seconds mem_mb workdir rpc_fd.
`-E -s` ignore the environment and user site-packages; `-I` is not used because it also
drops the script's own directory from sys.path, where hermes_tools.py sits.

The rlimits are per PROCESS, not per slot: RLIMIT_NPROC bounds how far a run can
multiply them, and the container's `mem_limit` bounds the total.
"""

import os
import resource
import sys

NPROC = 8  # numeric thread pools are pinned to one thread; a script needs no subprocesses
FSIZE = 50 * 2**20
NOFILE = 64
OOM_SCORE_ADJ = "/proc/self/oom_score_adj"


def _oom_first() -> None:
    """Make the run the OOM killer's first choice, ahead of the daemon.

    Raising the score needs no privilege. Lowering it below the inherited floor
    (`oom_score_adj_min`: 0 unless compose sets one) needs CAP_SYS_RESOURCE, which
    nothing in the container holds, so a run can at most bring itself back level with
    the daemon, never below it. Best effort: the runner's correctness does not depend
    on it, and a dev box may refuse the write."""
    try:
        with open(OOM_SCORE_ADJ, "w", encoding="ascii") as fh:
            fh.write("1000")
    except OSError:
        pass


def main(argv):
    uid, cpu, mem_mb, workdir, rpc_fd = int(argv[0]), int(argv[1]), int(argv[2]), argv[3], argv[4]
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 2**20, mem_mb * 2**20))
    resource.setrlimit(resource.RLIMIT_NPROC, (NPROC, NPROC))
    resource.setrlimit(resource.RLIMIT_FSIZE, (FSIZE, FSIZE))
    resource.setrlimit(resource.RLIMIT_NOFILE, (NOFILE, NOFILE))
    # A core dump would write a process image that holds tool data.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _oom_first()  # before the uid drop, which leaves /proc/self root's until the exec
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
