#!/usr/bin/env python3
"""nr-purge — NRDeepSeek 프롬프트 인젝트 관리 CLI.

dsh-purge 가 Harness 에 제공하는 것 중 봇에 의미가 있는 것만 옮긴 최소판:
상태 보기(status), 본문 보기(show), 적용(apply), 켜고 끄기(on/off),
되돌리기(revert), 백업 목록(backups), 차이(diff).

  python3 nr_purge.py status
  python3 nr_purge.py show
  python3 nr_purge.py apply ./new-prompt.md
  python3 nr_purge.py apply --from-dsh          # ~/.dsh/prompt-inject.md 를 그대로 가져온다
  python3 nr_purge.py off | on
  python3 nr_purge.py backups | diff | revert [--to 2]

적용·끄기·되돌리기 전에 현재 파일을 자동 백업한다(데이터 디렉터리의 .nr-purge/).
봇은 재시작 없이 다음 요청부터 새 본문을 쓴다.
"""

import argparse
import datetime
import difflib
import os
import re
import shutil
import sys

from prompt_inject import DEFAULT_FILENAME, resolve_inject_path, read_snapshot

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.environ.get("CONFIG_FILE", os.path.join(BASE_DIR, "server.json"))
DATA_DIR = os.path.dirname(CONFIG_FILE)
BACKUP_DIRNAME = ".nr-purge"
DSH_INJECT = os.path.expanduser("~/.dsh/prompt-inject.md")


def inject_path():
    return resolve_inject_path(BASE_DIR, DATA_DIR)


def read_text(path):
    """깨진 파일(비 UTF-8)이어도 예외 없이 읽는다 — CLI 가 트레이스백으로 죽지 않게."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def backup_dir():
    return os.path.join(DATA_DIR, BACKUP_DIRNAME)


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def list_backups():
    directory = backup_dir()
    if not os.path.isdir(directory):
        return []
    names = sorted(name for name in os.listdir(directory) if name.endswith(".md"))
    return [os.path.join(directory, name) for name in names]


def make_backup(path):
    """현재 파일을 백업한다. 파일이 없으면 아무것도 하지 않는다."""
    if not os.path.exists(path):
        return None
    directory = backup_dir()
    os.makedirs(directory, exist_ok=True)
    target = os.path.join(directory, "prompt-inject-%s.md" % stamp())
    suffix = 1
    while os.path.exists(target):
        target = os.path.join(directory, "prompt-inject-%s-%d.md" % (stamp(), suffix))
        suffix += 1
    shutil.copy2(path, target)
    return target


def write_atomic(path, text):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temp, path)


def set_enabled(path, enabled):
    """front matter 의 enabled 값만 바꾼다. front matter 가 없으면 새로 만든다."""
    raw = read_text(path) if os.path.exists(path) else ""

    match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?", raw, re.S)
    value = "true" if enabled else "false"
    if match:
        block = match.group(1)
        if re.search(r"^[ \t]*enabled[ \t]*:", block, re.M):
            block = re.sub(r"^[ \t]*enabled[ \t]*:.*$", "enabled: %s" % value, block, flags=re.M)
        else:
            block = "enabled: %s\n%s" % (value, block)
        write_atomic(path, "---\n%s\n---\n%s" % (block, raw[match.end():]))
    else:
        write_atomic(path, "---\nenabled: %s\n---\n%s" % (value, raw))


def print_status():
    path = inject_path()
    snapshot = read_snapshot(path)
    backups = list_backups()

    print("nr-purge 상태 / status")
    print("· 데이터 디렉터리 / data: %s" % DATA_DIR)
    print("· 인젝트 파일 / inject: %s" % path)
    if not snapshot.exists:
        print("· 상태: 없음 — 기본 프롬프트만으로 동작")
        shipped = os.path.join(BASE_DIR, DEFAULT_FILENAME)
        if os.path.exists(shipped) and os.path.abspath(shipped) != os.path.abspath(path):
            print("  켜기: python3 nr_purge.py apply   (이미지에 든 기본 본문을 %s 로 복사)" % path)
        else:
            print("  만들기: 본문을 %s 에 두면 다음 요청부터 적용된다" % path)
    elif snapshot.error:
        print("· 상태: 읽기 실패 — %s (봇은 기본 프롬프트로 계속 동작)" % snapshot.error)
    else:
        state = "적용 중" if snapshot.active else ("꺼짐(enabled: false)" if not snapshot.enabled else "빈 파일")
        print("· 상태: %s" % state)
        print("· 본문: %s자 · sha256 %s · mtime %s" % (
            format(snapshot.size, ","), snapshot.short_sha,
            datetime.datetime.fromtimestamp(snapshot.mtime_ns / 1e9).strftime("%Y-%m-%d %H:%M:%S")))
        if snapshot.title:
            print("· 제목: %s" % snapshot.title)
    print("· 백업: %d개 (%s)" % (len(backups), backup_dir()))
    print("· 반영: 파일을 고치면 다음 요청부터 자동 반영 (봇 재시작 불필요)")
    print("· 확인: 봇 로그의 [PURGE] 줄, 또는 마지막 답이 바뀌었는지")
    return 0


def print_show():
    path = inject_path()
    if not os.path.exists(path):
        print("[WARN] 인젝트 파일이 없습니다: %s" % path)
        return 1
    sys.stdout.write(read_text(path))
    return 0


def load_source(args):
    if args.from_dsh:
        source = os.path.expanduser(args.file or DSH_INJECT)
        label = "~/.dsh/prompt-inject.md"
    elif args.file:
        source = args.file
        label = args.file
    else:
        # 인자 없이 apply → 이미지/저장소에 든 기본 본문을 데이터 디렉터리로 복사(초기 설치)
        source = os.path.join(BASE_DIR, DEFAULT_FILENAME)
        label = source
    if not source or not os.path.exists(source):
        print("[ERROR] 원본 파일을 찾을 수 없습니다: %s" % label)
        return None, None
    return source, read_text(source)


def cmd_apply(args):
    source, text = load_source(args)
    if text is None:
        return 1
    target = inject_path()
    if os.path.abspath(source) == os.path.abspath(target):
        print("[WARN] 원본과 대상이 같습니다: %s" % target)
        return 1
    backup = make_backup(target)
    write_atomic(target, text)
    snapshot = read_snapshot(target)
    print("[OK] 적용: %s → %s" % (source, target))
    print("· 본문 %s자 · sha256 %s" % (format(snapshot.size, ","), snapshot.short_sha))
    print("· 백업: %s" % (backup or "없음 (이전 파일 없음)"))
    if not snapshot.active:
        print("[WARN] 본문이 비었거나 enabled: false 입니다 — 지금은 인젝트가 꺼진 상태로 동작합니다.")
    if args.from_dsh:
        print("· 주의: ~/.dsh/prompt-inject.md 는 Harness 전용 문구(도구 이름·경로·CTF 라우팅)를 포함합니다.")
        print("  봇에는 그대로 남으면 잡음이 되므로, 필요 없는 절은 직접 지우세요.")
    return 0


def cmd_toggle(args, enabled):
    path = inject_path()
    if not os.path.exists(path):
        print("[ERROR] 인젝트 파일이 없습니다: %s" % path)
        return 1
    backup = make_backup(path)
    set_enabled(path, enabled)
    snapshot = read_snapshot(path)
    print("[OK] %s: %s" % ("켜짐" if enabled else "꺼짐", path))
    print("· 상태: %s · 백업 %s" % ("적용 중" if snapshot.active else "미적용", backup or "없음"))
    return 0


def cmd_backups(_args):
    backups = list_backups()
    if not backups:
        print("· 백업 없음 (%s)" % backup_dir())
        return 0
    print("· 백업 %d개 (%s)" % (len(backups), backup_dir()))
    for index, path in enumerate(backups, 1):
        size = os.path.getsize(path)
        print("  [%d] %s · %s자" % (index, os.path.basename(path), format(size, ",")))
    return 0


def cmd_revert(args):
    backups = list_backups()
    if not backups:
        print("[ERROR] 되돌릴 백업이 없습니다: %s" % backup_dir())
        return 1
    index = args.to if args.to else len(backups)
    if index < 1 or index > len(backups):
        print("[ERROR] 백업 번호 범위 밖: %d (1~%d)" % (index, len(backups)))
        return 1
    source = backups[index - 1]
    target = inject_path()
    current = make_backup(target)
    shutil.copy2(source, target)
    snapshot = read_snapshot(target)
    print("[OK] 되돌림: [%d] %s → %s" % (index, os.path.basename(source), target))
    print("· 본문 %s자 · sha256 %s" % (format(snapshot.size, ","), snapshot.short_sha))
    if current:
        print("· 되돌리기 전 상태도 백업했습니다: %s" % os.path.basename(current))
    return 0


def cmd_diff(_args):
    path = inject_path()
    backups = list_backups()
    if not backups:
        print("[ERROR] 비교할 백업이 없습니다.")
        return 1
    if not os.path.exists(path):
        print("[ERROR] 현재 파일이 없습니다: %s" % path)
        return 1
    old = read_text(backups[-1]).splitlines(keepends=True)
    new = read_text(path).splitlines(keepends=True)
    diff = list(difflib.unified_diff(old, new, fromfile=os.path.basename(backups[-1]),
                                     tofile=os.path.basename(path)))
    if not diff:
        print("· 차이 없음 (마지막 백업과 동일)")
        return 0
    sys.stdout.writelines(diff)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="nr_purge.py", description="NRDeepSeek 프롬프트 인젝트 관리")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="인젝트 상태 한 화면").set_defaults(func=lambda _a: print_status())
    sub.add_parser("show", help="본문 출력").set_defaults(func=lambda _a: print_show())

    apply_parser = sub.add_parser("apply", help="파일을 인젝트로 적용 (자동 백업)")
    apply_parser.add_argument("file", nargs="?", help="원본 경로")
    apply_parser.add_argument("--from-dsh", action="store_true", help="~/.dsh/prompt-inject.md 를 그대로 가져온다")
    apply_parser.set_defaults(func=cmd_apply)

    on_parser = sub.add_parser("on", help="인젝트 켜기 (enabled: true)")
    on_parser.set_defaults(func=lambda a: cmd_toggle(a, True))
    off_parser = sub.add_parser("off", help="인젝트 끄기 (enabled: false)")
    off_parser.set_defaults(func=lambda a: cmd_toggle(a, False))

    sub.add_parser("backups", help="백업 목록").set_defaults(func=cmd_backups)

    revert_parser = sub.add_parser("revert", help="백업으로 되돌리기")
    revert_parser.add_argument("--to", type=int, help="백업 번호 (기본: 가장 최근)")
    revert_parser.set_defaults(func=cmd_revert)

    sub.add_parser("diff", help="마지막 백업과 현재의 차이").set_defaults(func=cmd_diff)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
