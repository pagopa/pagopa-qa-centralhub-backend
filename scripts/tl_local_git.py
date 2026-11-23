"""Comandi per la modalità Git locale del Test Lifecycle (solo sviluppo).

Lavorano sul clone indicato da TL_LOCAL_REPO_PATH (o --repo). Nulla viene inviato a GitHub.

Uso:
    uv run python -m scripts.tl_local_git prs                      # elenca le PR simulate
    uv run python -m scripts.tl_local_git merge 1                  # merge della PR #1 su main
    uv run python -m scripts.tl_local_git close 1                  # chiude la PR #1 senza merge
    uv run python -m scripts.tl_local_git show main <percorso>     # stampa un file da un branch
    uv run python -m scripts.tl_local_git write main <percorso> <file-locale> [-m messaggio]
                                                                   # "qualcuno modifica main"
    uv run python -m scripts.tl_local_git rm main <percorso>       # "qualcuno cancella il file"
    uv run python -m scripts.tl_local_git reset                    # pulizia: branch qa-hub/*,
                                                                   # PR simulate, main = origin/main
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import settings
from app.services.test_lifecycle.git_client import TlGitError
from app.services.test_lifecycle.local_git import (
    LocalGitClient,
    _load_pulls,
    local_close,
    local_merge,
    local_remove,
    local_reset,
    local_write,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tl_local_git", description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=settings.tl_local_repo_path, help="percorso del clone")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("prs")
    for name in ("merge", "close"):
        sub.add_parser(name).add_argument("number", type=int)
    show = sub.add_parser("show")
    show.add_argument("branch")
    show.add_argument("path")
    write = sub.add_parser("write")
    write.add_argument("branch")
    write.add_argument("path")
    write.add_argument("source", help="file locale con il nuovo contenuto")
    write.add_argument("-m", "--message", default="Modifica di test (locale)")
    rm = sub.add_parser("rm")
    rm.add_argument("branch")
    rm.add_argument("path")
    rm.add_argument("-m", "--message", default="Rimozione di test (locale)")
    sub.add_parser("reset")
    args = parser.parse_args(argv)

    if not args.repo:
        print("Indica il clone con --repo o TL_LOCAL_REPO_PATH", file=sys.stderr)
        return 2
    repo = args.repo
    try:
        if args.cmd == "prs":
            for p in _load_pulls(repo):
                print(f"#{p['number']} [{p['state']}] {p['head']} -> {p['base']}  {p['title']}")
        elif args.cmd == "merge":
            print(f"PR #{args.number} integrata: {local_merge(repo, args.number)}")
        elif args.cmd == "close":
            local_close(repo, args.number)
            print(f"PR #{args.number} chiusa senza merge")
        elif args.cmd == "show":
            got = LocalGitClient(repo)._get_file(args.path, args.branch)
            if got is None:
                print(f"{args.path} non esiste su {args.branch}", file=sys.stderr)
                return 1
            sys.stdout.write(got.text)
        elif args.cmd == "write":
            text = Path(args.source).read_text(encoding="utf-8")
            sha = local_write(repo, args.branch, args.path, text, args.message)
            print(f"Commit su {args.branch}: {sha}")
        elif args.cmd == "rm":
            sha = local_remove(repo, args.branch, args.path, args.message)
            print(f"Commit su {args.branch}: {sha}")
        elif args.cmd == "reset":
            local_reset(repo)
            print("Pulizia fatta: branch qa-hub/* e PR rimossi, main riportato su origin/main")
    except TlGitError as exc:
        print(f"Errore: {exc.message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
