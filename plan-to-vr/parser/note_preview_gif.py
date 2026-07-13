#!/usr/bin/env python3
"""Build a small looping GIF preview from a VR-note webm recording.

GitHub renders committed GIFs inline in issue bodies (they proxy through
its image cache), but never renders a video player for a committed
video - that needs the web-composer attachment upload, which no API
exposes. So to show a note's recording *in the issue*, transcode a short
looping GIF and embed it; the full-quality webm stays linked below.

Emits nothing (exit 2) for an audio-only note, so callers can skip the
embed cleanly.

Usage:
    python note_preview_gif.py note.webm preview.gif
        [--width 420] [--max-frames 24] [--stride 5] [--ms 140]
"""
import argparse
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("webm")
    ap.add_argument("gif")
    ap.add_argument("--width", type=int, default=420)
    ap.add_argument("--max-frames", type=int, default=24)
    ap.add_argument("--stride", type=int, default=5)   # keep every Nth frame
    ap.add_argument("--ms", type=int, default=140)      # frame duration
    ap.add_argument("--colors", type=int, default=128)
    args = ap.parse_args()

    import av
    from PIL import Image

    # audio-only, empty, or truncated recordings just mean "no preview" -
    # exit 2 so the caller skips the embed instead of failing the note
    try:
        container = av.open(args.webm)
        vstreams = [s for s in container.streams if s.type == "video"]
    except Exception as e:
        print(f"unreadable/empty note ({e}): no preview", file=sys.stderr)
        sys.exit(2)
    if not vstreams:
        print("audio-only note: no video track", file=sys.stderr)
        sys.exit(2)

    frames = []
    i = 0
    try:
        for f in container.decode(vstreams[0]):
            if i % args.stride == 0:
                im = f.to_image()
                im.thumbnail((args.width, args.width), Image.LANCZOS)
                frames.append(im.convert(
                    "P", palette=Image.ADAPTIVE, colors=args.colors))
                if len(frames) >= args.max_frames:
                    break
            i += 1
    except Exception as e:
        if not frames:
            print(f"decode failed ({e}): no preview", file=sys.stderr)
            sys.exit(2)   # partial decode with some frames is still usable

    if not frames:
        print("no frames decoded", file=sys.stderr)
        sys.exit(2)

    frames[0].save(args.gif, save_all=True, append_images=frames[1:],
                   loop=0, duration=args.ms, optimize=True, disposal=2)
    import os
    kb = os.path.getsize(args.gif) // 1024
    print(f"wrote {args.gif}: {len(frames)} frames, {kb} KB")


if __name__ == "__main__":
    main()
