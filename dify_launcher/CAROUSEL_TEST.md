# panda-carousel — how to re-run tests

Usage, aspect ratios, dual-mode stills revise, and **recorded job ids** live in
[`CAROUSEL.md`](CAROUSEL.md).

Automated handshake (no server):

```bash
python3 dify_launcher/test_dify_flow.py
python3 dify_launcher/test_claude_adapter.py
python3 -m pytest tests/contracts/test_panda_carousel_pipeline.py -q
```

HTTP walks: isolated launcher on **:8600** — see [`CAROUSEL.md`](CAROUSEL.md) § Isolated test launcher.

## Do not

- Bind the test launcher to **8501** (live Dify).
- Commit `data/carousel-*` or `projects-carousel-claude/` (local artifacts).
- Expect mock stills to look like Higgsfield panda art.
