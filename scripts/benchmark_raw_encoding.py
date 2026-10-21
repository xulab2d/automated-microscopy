"""Compare lossless TIFF encoders on saved data without touching acquisition."""
from pathlib import Path
import argparse
import json
import sys
import time

import cv2
import numpy as np


def benchmark(path, repetitions=3):
    raw = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if raw is None:
        raise ValueError(f"Cannot read {path}")
    results = []
    for name, compression in [("lzw", 5), ("deflate", 32946), ("zstd", 50000)]:
        timings = []
        try:
            for _ in range(repetitions):
                started = time.perf_counter()
                success, encoded = cv2.imencode(".tiff", raw, [cv2.IMWRITE_TIFF_COMPRESSION, compression])
                timings.append(time.perf_counter() - started)
                if not success:
                    raise RuntimeError("Encoder returned failure")
            started = time.perf_counter()
            decoded = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
            decode_s = time.perf_counter() - started
            identical = decoded is not None and decoded.dtype == raw.dtype and np.array_equal(decoded, raw)
            if not identical:
                raise RuntimeError("Lossless pixel roundtrip failed")
            results.append(dict(compression=name, bytes=int(encoded.nbytes),
                                encode_median_s=float(np.median(timings)), decode_s=decode_s,
                                pixels_identical=True))
        except (cv2.error, RuntimeError) as exc:
            results.append(dict(compression=name, error=str(exc)))
    return dict(source=str(path.resolve()), shape=list(raw.shape), dtype=str(raw.dtype),
                raw_bytes=int(raw.nbytes), repetitions=repetitions, results=results,
                qualification="In-memory encoding of one saved frame; excludes filesystem and hardware. No acquisition settings changed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("frame", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = benchmark(args.frame)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
