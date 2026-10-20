# Sourced at the start of every gipper job on a pod: tools jobs can rely on.
# Idempotent and quiet; anything heavier belongs in the job's own setup step.
export PATH="$HOME/.local/bin:$PATH"
export PIP_DISABLE_PIP_VERSION_CHECK=1
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1
fi
# Weights and environments persist on a network volume when one is attached.
if [ -d /workspace ] && [ -w /workspace ]; then
  export HF_HOME="${HF_HOME:-/workspace/hf}" UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/uv-cache}"
fi
# The PyTorch wheel index that matches this host's driver: jobs install torch with
#   uv pip install torch --index-url "$GIPPER_TORCH_INDEX"
_cuda=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: \([0-9]*\)\.\([0-9]*\).*/\1\2/p' | head -1)
if   [ "${_cuda:-0}" -ge 130 ]; then GIPPER_TORCH_INDEX=https://download.pytorch.org/whl/cu130
elif [ "${_cuda:-0}" -ge 128 ]; then GIPPER_TORCH_INDEX=https://download.pytorch.org/whl/cu128
elif [ "${_cuda:-0}" -ge 126 ]; then GIPPER_TORCH_INDEX=https://download.pytorch.org/whl/cu126
else GIPPER_TORCH_INDEX=https://download.pytorch.org/whl/cu124; fi
export GIPPER_TORCH_INDEX
echo "gipper: host CUDA ${_cuda:-?}, torch index $GIPPER_TORCH_INDEX"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/gipper: gpu /'
