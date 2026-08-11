#!/usr/bin/env bash
set -Eeuo pipefail

: "${MODEL:?MODEL is required}"
: "${HF_REPO:?HF_REPO is required}"
: "${REVISION:?REVISION is required}"
: "${FILE_PATH:?FILE_PATH is required}"
: "${EXPECTED_SIZE:?EXPECTED_SIZE is required}"
: "${EXPECTED_SHA256:?EXPECTED_SHA256 is required}"
: "${EXPECTED_PART_COUNT:?EXPECTED_PART_COUNT is required}"
: "${CHUNK_BYTES:?CHUNK_BYTES is required}"
: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"
: "${GH_TOKEN:?GH_TOKEN is required}"

stage="${RUNNER_TEMP}/glm-mirror-${MODEL}-${RANDOM}"
fifo="${stage}/hash.fifo"
hash_file="${stage}/whole.sha256"
mkdir -p "$stage"
cleanup() { rm -rf "$stage"; }
trap cleanup EXIT

available=$(df --output=avail -B1 "$RUNNER_TEMP" | tail -1 | tr -d ' ')
required=$((EXPECTED_SIZE + 1000000000))
if (( available < required )); then
  echo "Not enough runner disk: need ${required}, have ${available}" >&2
  exit 1
fi

asset_base="${FILE_PATH//\//__}"
url="https://huggingface.co/${HF_REPO}/resolve/${REVISION}/${FILE_PATH}?download=true"
download_complete=false

# A retry must restart the entire stream. Retrying curl in-place would append a
# second response to already emitted bytes and produce a corrupt split set.
for download_attempt in 1 2 3; do
  rm -f "$fifo" "$hash_file" "${stage}/${asset_base}.part"*
  mkfifo "$fifo"
  sha256sum < "$fifo" > "$hash_file" &
  hash_pid=$!

  echo "Streaming ${HF_REPO}@${REVISION}:${FILE_PATH} (attempt ${download_attempt}/3)"
  pipeline_ok=true
  if ! curl \
    --fail-with-body \
    --location \
    --connect-timeout 30 \
    --speed-limit 1024 \
    --speed-time 180 \
    --silent \
    --show-error \
    "$url" \
    | tee "$fifo" \
    | split --bytes="$CHUNK_BYTES" --numeric-suffixes=0 --suffix-length=2 - "${stage}/${asset_base}.part"; then
    pipeline_ok=false
  fi
  if ! wait "$hash_pid"; then
    pipeline_ok=false
  fi
  rm -f "$fifo"

  actual_sha256=$(cut -d' ' -f1 "$hash_file" 2>/dev/null || true)
  mapfile -t parts < <(find "$stage" -maxdepth 1 -type f -name "${asset_base}.part*" | sort)
  split_size=$(python3 - "$stage" "$asset_base" <<'PY'
import glob
import os
import sys
print(sum(os.path.getsize(p) for p in glob.glob(os.path.join(sys.argv[1], sys.argv[2] + ".part*"))))
PY
  )

  if [[ "$pipeline_ok" == true \
        && "$actual_sha256" == "$EXPECTED_SHA256" \
        && "${#parts[@]}" -eq "$EXPECTED_PART_COUNT" \
        && "$split_size" -eq "$EXPECTED_SIZE" ]]; then
    download_complete=true
    break
  fi

  echo "Attempt ${download_attempt} failed validation:" >&2
  echo "  pipeline_ok=${pipeline_ok}" >&2
  echo "  sha256=${actual_sha256:-missing}" >&2
  echo "  parts=${#parts[@]}" >&2
  echo "  bytes=${split_size}" >&2
  if [[ "$download_attempt" -lt 3 ]]; then
    sleep $((download_attempt * 30))
  fi
done

if [[ "$download_complete" != true ]]; then
  echo "Unable to produce a verified split set after 3 attempts" >&2
  exit 1
fi

upload_one() {
  local part="$1"
  local attempt
  for attempt in 1 2 3 4 5 6; do
    echo "Uploading $(basename "$part") (attempt ${attempt}/6)"
    if gh release upload "$RELEASE_TAG" "$part" \
      --repo "$GITHUB_REPOSITORY" --clobber; then
      return 0
    fi
    sleep $((attempt * 20))
  done
  return 1
}

for part in "${parts[@]}"; do
  upload_one "$part"
  rm -f "$part"
done

{
  echo "### Mirrored ${MODEL}"
  echo "- File: \`${FILE_PATH}\`"
  echo "- Bytes: \`${EXPECTED_SIZE}\`"
  echo "- Upstream SHA-256: \`${EXPECTED_SHA256}\`"
  echo "- Release parts: \`${EXPECTED_PART_COUNT}\`"
} >> "$GITHUB_STEP_SUMMARY"
