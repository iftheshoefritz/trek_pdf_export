# Parallelizing the batch extraction

Notes for a future change to `extract_cards.py` that processes multiple PDFs concurrently. Target: ~4-8× wall-clock speedup on M-series Macs for large batches like the 12GB Dropbox virtual-set library.

## Where to change

Only `main()` in `extract_cards.py` needs to change. The unit of parallelism is the existing `process_pdf()` call — one worker per PDF, no finer.

`process_pdf()` is already safe to call concurrently:

- It creates its own `tempfile.TemporaryDirectory` for per-page rasters (no shared scratch space)
- It writes outputs by canonical card ID, not by sequence index — different PDFs produce different filenames (modulo the errata case, see below)
- It returns `(n_saved, errors)` cleanly — no shared mutable state

The OCR fallback inside `process_pdf()` only votes across cards within a single PDF, so it remains safe.

## Architecture sketch

```python
# After building `pdfs`, partition by errata pattern.
errata = [p for p in pdfs if p.name.startswith("zzz_")]
non_errata = [p for p in pdfs if not p.name.startswith("zzz_")]

# Refactor the existing per-PDF loop body into a worker function that
# takes a single PDF and returns (pdf, n_saved, errors, buffered_output).
# Currently the loop body prints directly to stdout; in parallel mode that
# would interleave between workers, so the worker should accumulate its
# prints into a list and the main thread emits them on completion.

def _process_one(pdf: Path, ...) -> tuple[Path, int, list[str], list[str]]:
    log = []
    log.append(f"{pdf.name}")
    # ... existing sidecar logic, with print() -> log.append() ...
    n_saved, errors = process_pdf(pdf, ...)
    log.append(f"  -> {n_saved} cards to {pdf_out_dir}")
    return pdf, n_saved, errors, log

# Non-errata: parallel pool.
if args.jobs > 1 and len(non_errata) > 1:
    from concurrent.futures import ProcessPoolExecutor, as_completed
    completed = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_process_one, pdf, ...): pdf for pdf in non_errata}
        for future in as_completed(futures):
            pdf, n_saved, errors, log = future.result()
            completed += 1
            print(f"[{completed}/{len(non_errata)}] " + log[0])
            for line in log[1:]:
                print(line)
            results.append((pdf, n_saved, errors))
else:
    # Existing sequential path, unchanged.
    ...

# Errata: always sequential, AFTER the pool drains. This is what makes the
# zzz_errata-overwrites-originals pattern correct under parallelism.
for pdf in errata:
    pdf, n_saved, errors, log = _process_one(pdf, ...)
    for line in log:
        print(line)
    results.append((pdf, n_saved, errors))
```

## New CLI flag

```python
p.add_argument("--jobs", type=int, default=1, metavar="N",
               help="Process N PDFs in parallel (default 1). Files matching "
                    "zzz_* are always processed serially after the parallel "
                    "batch so they overwrite originals correctly.")
```

Default `1` preserves current behavior exactly. The user opts in.

## The errata-ordering caveat

This is the only non-obvious correctness issue. The current single-threaded loop processes PDFs in `sorted()` order, which puts `zzz_errata.pdf` last, which is what makes "errata overwrites originals" work via filename collision.

Naive parallelization breaks this:
- If all 85 PDFs go into a pool, errata cards and original-set cards can be written in any order
- Whichever finishes first wins; the result depends on PDF size and CPU scheduling, not file order

Fix: detect `zzz_*` files at the top of `main()`, exclude them from the pool, and run them serially after the pool drains. The errata file is small (one PDF) so serial processing of it adds ~minutes, not hours.

Alternative: don't partition; instead have `process_pdf` write to a temp dir and then atomically `mv` files in a separate phase. More complex, no real benefit unless errata grows large.

## Pitfalls and design choices

1. **Output interleaving**: workers must buffer their prints. Easiest is `log: list[str]`; main thread emits on completion in batch.

2. **`--verbose` with parallel**: per-page output is dense and would be unreadable if interleaved across 8 workers. Same buffering approach handles it — each worker accumulates its full log, main thread emits it as one block. Order across workers is non-deterministic (whatever finishes first) but each PDF's output stays internally coherent.

3. **Progress reporting**: change `[1/85]` to `[3/85 complete]` since indices complete out of order. Use a completion counter, not the original sort index.

4. **Memory**: each 800-DPI letter-page raster is ~150MB; PIL intermediates add more. With `--jobs 8` that's a ~1.2GB peak just for rasters. Fine on 16GB+ machines; could be a problem with `--bleed` (extra buffers) or higher DPI. Default `--jobs 1` keeps this opt-in.

5. **Disk I/O contention**: writing 9 PNGs per page × 8 workers concurrently to the same directory may bottleneck SSDs less than expected (modern SSDs are fast enough) but could be the actual ceiling on Dropbox-synced folders if the Dropbox daemon is also pushing each file as it lands.

6. **CTRL-C**: `ProcessPoolExecutor` with `with`-block context manager handles `KeyboardInterrupt` cleanly via `shutdown(wait=False, cancel_futures=True)` (Python 3.9+). Confirm the cancel_futures behavior; older versions may leak workers.

7. **Pickling**: function args passed to `pool.submit` must be picklable. Everything currently passed (Path, str, int, bool, tuple[str,int]|None) is fine. No closures over module-level state.

8. **Process startup cost**: `ProcessPoolExecutor` forks workers on macOS up through 3.13 (spawn from 3.14). Spawn is slower to start but safer for state isolation; fork inherits memory. For an 85-PDF batch the startup cost is amortized — irrelevant. For a 2-PDF batch the overhead might dominate; that's why the parallel path should only kick in when `args.jobs > 1 and len(non_errata) > 1`.

## Testing approach

1. Run with `--jobs 1` on a known input. Output should byte-identical match the current behavior. (`diff` filenames and image hashes against a sequential reference run.)
2. Run with `--jobs 4` on a small subset (4-5 PDFs, including one errata-style `zzz_*`). Verify:
   - Same filenames produced
   - Errata cards correctly overwrote originals (same canonical IDs, errata image bytes win)
   - Exit code 1 when any PDF errored, 0 otherwise
3. Run with `--jobs 8` on the full Dropbox set; measure wall-clock vs. sequential baseline.

## Out of scope (don't bother)

- **Per-page parallelism within a PDF**: the per-PDF concurrency already saturates available cores. Page-level adds complexity (shared tempdir, cross-page OCR voting) for no real gain.
- **Auto-tuning `--jobs`** based on `os.cpu_count()`: explicit is fine. The user knows their machine.
- **A worker thread pool instead of process pool**: `pdftoppm`/`pdftotext` are subprocesses, so the GIL isn't the issue, but the PIL/numpy work inside Python *is* GIL-bound. Process pool is the right call.
