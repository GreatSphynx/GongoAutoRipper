"""Stand-in for makemkvcon used by the tests: prints robot-mode output for a
fake TV DVD and writes small fake .mkv files.

Set FAKE_FAIL_ONCE to a marker-file path to make title 1 fail on its first rip,
and FAKE_CALL_LOG to a file path to record each rip command.
"""

import os
import sys
from pathlib import Path

# (duration in seconds, segment map) for each title on the fake disc:
# three episodes, a short extra, a "play all" title and a duplicate of episode 1.
TITLES = [(26 * 60, "1-3"), (26 * 60 + 10, "4-6"), (120, "7"), (25 * 60 + 50, "8-10"),
          (78 * 60, "1-10"), (26 * 60, "1-3")]


def fmt(seconds):
    return f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def content(duration):
    return f"duration={duration}"


def main(argv):
    minlength = 120
    args = []
    for a in argv:
        if a.startswith("--minlength="):
            minlength = int(a.split("=", 1)[1])
        elif not a.startswith("-"):
            args.append(a)
    visible = [(*t, number) for number, t in enumerate(TITLES, start=1) if t[0] >= minlength]

    print('MSG:1005,0,1,"MakeMKV v1.18.4 win(x64-release) started","%1 started","MakeMKV"')
    if args[0] == "info" and args[1] == "disc:9999":
        print('DRV:0,2,999,1,"BD-RE FAKE DRIVE","FAKE_DISC","E:"')
        print('DRV:1,256,999,0,"","",""')
        return 0
    if args[0] == "info":
        print('CINFO:2,0,"FAKE_DISC"')
        print(f"TCOUNT:{len(visible)}")
        for i, (duration, segments, number) in enumerate(visible):
            print(f'TINFO:{i},8,0,"5"')
            print(f'TINFO:{i},9,0,"{fmt(duration)}"')
            print(f'TINFO:{i},10,0,"1.2 GB"')
            print(f'TINFO:{i},11,0,"{len(content(duration))}"')
            print(f'TINFO:{i},24,0,"{number:02}"')
            print(f'TINFO:{i},26,0,"{segments}"')
            print(f'TINFO:{i},27,0,"FAKE_DISC_t{i:02}.mkv"')
            print(f'MSG:3028,0,3,"Title #{number} was added","Title #%1 was added","{number}"')
        return 0
    if args[0] == "mkv":
        which, out = args[2], Path(args[3])
        if log := os.environ.get("FAKE_CALL_LOG"):
            with open(log, "a") as f:
                f.write(which + "\n")
        marker = os.environ.get("FAKE_FAIL_ONCE")
        titles = range(len(visible)) if which == "all" else [int(which)]
        failed = 0
        for p in (0, 16384, 32768, 65536):
            print(f"PRGV:{p},{p},65536")
        for title in titles:
            name = f"FAKE_DISC_t{title:02}.mkv"
            if marker and title == 1 and not os.path.exists(marker):
                Path(marker).write_text("failed once")
                print(f'MSG:5003,0,2,"Failed to save title {title} to file {name}","",""')
                failed += 1
                continue
            (out / name).write_text(content(visible[title][0]))
        saved = len(titles) - failed
        print(f'MSG:5037,0,2,"Copy complete. {saved} titles saved, {failed} failed.","",""')
        return 1 if failed else 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
