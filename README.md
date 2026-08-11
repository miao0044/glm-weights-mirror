# GLM Weight Mirror

This repository creates an independent, byte-for-byte GitHub Release mirror of
four public Z.ai models. Weight data moves directly from pinned Hugging Face
commits to GitHub-hosted runners and then to GitHub Releases; it does not pass
through the operator's computer.

| Model | Pinned Hugging Face revision | Approx. weight size | Release |
| --- | --- | ---: | --- |
| GLM-5.1 | `26e1bd6e011feb778d25ae34b09b07074139d92d` | 1.508 TB | `glm-5.1-26e1bd6` |
| GLM-5.1-FP8 | `f396cf805182f4ca10fa675e1a99815b3ca384db` | 756 GB | `glm-5.1-fp8-f396cf8` |
| GLM-5.2 | `b4734de4facf877f85769a911abafc5283eab3d9` | 1.507 TB | `glm-5.2-b4734de` |
| GLM-5.2-FP8 | `ba978f7d347eaf65d22f1a86833408afdb953541` | 756 GB | `glm-5.2-fp8-ba978f7` |

## How the mirror works

GitHub repository files cannot hold multi-gigabyte model shards. The manual
workflow streams each upstream `.safetensors` file at a pinned revision,
verifies its upstream SHA-256, splits it into deterministic 1.9 GB pieces, and
uploads those pieces as Release assets. GitHub records a SHA-256 digest for each
asset. Non-weight files, the upstream license, tokenizer, configuration, model
card, and the pinned manifest are packaged separately.

Each workflow range is divided into 12 small, resumable lanes. A lane checks
the Release before doing work and skips every shard whose expected parts are
already present with the correct sizes and GitHub digests. Failed lanes can be
rerun without restarting successful shards. A companion `workflow_run`
automation retries failed or timed-out lanes up to three times after a short
cooldown. A daily audit waits until no mirror run is active, verifies all
expected assets, dispatches resumable repair lanes if needed, and disables
itself only after all four releases and the final restore proof are complete.

## Restore

Install Python 3 and the GitHub CLI, authenticate with `gh auth login`, then run:

```bash
python scripts/restore_model.py \
  --repo miao0044/glm-weights-mirror \
  --model GLM-5.2-FP8 \
  --output /path/with/enough/free/space
```

The restore tool downloads one Release part at a time, verifies GitHub's digest,
reassembles every original file, and finally verifies the upstream whole-file
SHA-256. It does not need a second full-model-sized temporary directory.

Before a local source copy is removed, the `Verify restored weights` workflow
performs the same check in streaming mode for every weight shard. It validates
each downloaded Release part against GitHub's SHA-256, validates the reconstructed
file against the pinned upstream SHA-256, and writes one proof marker per shard.
`ALL_MODELS_RESTORE_VERIFIED.json` is created only after all 847 weight files and
all four Releases pass.

Do not delete another known-good copy until `verify_release.py` reports the
release complete and a restore test has succeeded.

To check current cloud-copy progress without treating incomplete releases as an
error:

```bash
python scripts/verify_release.py \
  --repo miao0044/glm-weights-mirror \
  --allow-incomplete
```

## Attribution and license

The mirrored models are from the public `zai-org` Hugging Face repositories and
declare the MIT license. Each metadata archive contains the upstream `LICENSE`
and model card. The orchestration and restore scripts in this repository are
also provided under the MIT license.
