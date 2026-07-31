# SWE-bench Environment Rollout

1. Choose a new immutable bundle prefix, for example:

   ```bash
   export BUNDLE_URI=s3://grl-swebench-lite/datasets/swebench-lite/dev-v0.0.15
   ```

2. Ensure the new bundle contains the current `tasks.jsonl`. Either generate it
   from the dataset or copy it from the currently active bundle.

3. Build and upload the updated Linux environment artifact:

   ```bash
   cd environments/swebench-lite/vms
   uv sync
   uv run vms build-environment --force --upload \
     --bundle-uri "$BUNDLE_URI"
   ```

4. Set `environment.bundle_uri` in `launcher/config.yaml` to the new bundle URI
   and deploy FULL.

5. Run a smoke rollout and confirm:

   ```bash
   pwd
   command -v bash
   command -v python
   ```

   Expected results:

   - `pwd` reports `/testbed`.
   - Bash resolves successfully.
   - Python resolves to `/opt/testbed/bin/python`.
