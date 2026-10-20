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
# The PyTorch wheel index that matches this host's *driver* (not what nvidia-smi
# reports as "CUDA Version": images with CUDA forward-compat libraries inflate
# that). Jobs install torch with: uv pip install torch --index-url "$GIPPER_TORCH_INDEX"
_drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | cut -d. -f1)
_drv=${_drv:-0}
if   [ "$_drv" -ge 580 ]; then _cuda=130
elif [ "$_drv" -ge 570 ]; then _cuda=128
elif [ "$_drv" -ge 560 ]; then _cuda=126
elif [ "$_drv" -ge 550 ]; then _cuda=124
else _cuda=121; fi
GIPPER_TORCH_INDEX=https://download.pytorch.org/whl/cu$_cuda
export GIPPER_TORCH_INDEX
echo "gipper: driver $_drv -> torch cu$_cuda ($GIPPER_TORCH_INDEX)"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/gipper: gpu /'
