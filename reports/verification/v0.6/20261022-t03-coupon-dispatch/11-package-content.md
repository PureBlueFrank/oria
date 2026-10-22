# Package content check

Command outputs from `make build` produced both `oria-0.1.0.tar.gz` and `oria-0.1.0-py3-none-any.whl`.

- The wheel contains `oria/orchestrator/job_coupon_dispatch.py` and `oria/storage/job_coupon_dispatch.py`.
- The source distribution contains both modules and `tests/integration/test_v06_job_coupon_dispatch.py`.
- The inspected source distribution contained no `.artifacts`, `.venv`, or `.env` paths. `uv build` warned that the cache directory was inside the source tree, so the produced archive was checked explicitly before accepting the build.
