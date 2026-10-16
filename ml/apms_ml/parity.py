# ml/apms_ml/parity.py
"""Verify the downloaded ONNX model matches its PyTorch checkpoint.

Run:  python -m apms_ml.parity --checkpoint ../models/best.pt \
          --onnx ../models/mnv3s-pklot-20260921.onnx

Runs 100 real validation patches, not random tensors: a wrong channel order or
a dropped normalisation can hide behind uniform noise. Exits non-zero on
divergence. 06_ENV_SETUP.md calls silent export breakage a real failure mode
that would poison every downstream number — this is the guard.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort
import torch
import torch.nn as nn
from torchvision.models import mobilenet_v3_small

from .paths import SPLITS_DIR

N_SAMPLES = 100
MAX_ABS_DELTA = 1e-4


class ServingModel(nn.Module):
    """Mirror of the class in ml/train/train_mobilenetv3.py. It is duplicated
    deliberately: the training script must stay self-contained for Colab, and
    this copy re-deriving the same result is itself part of the check."""

    def __init__(self, backbone, mean, std):
        super().__init__()
        self.backbone = backbone
        self.register_buffer("bgr_to_rgb", torch.tensor([2, 1, 0], dtype=torch.long))
        self.register_buffer("mean", torch.tensor(mean).view(1, 3, 1, 1) * 255.0)
        self.register_buffer("std", torch.tensor(std).view(1, 3, 1, 1) * 255.0)

    def forward(self, x):
        x = x.index_select(1, self.bgr_to_rgb)
        logits = self.backbone((x - self.mean) / self.std)
        return torch.softmax(logits, dim=1)[:, 1:2]


def load_patches(input_size: int) -> np.ndarray:
    manifest = SPLITS_DIR / "same_lot_val.csv"
    if not manifest.exists():
        raise SystemExit(f"{manifest} missing — run `python -m apms_ml.splits`.")
    with open(manifest, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    stride = max(len(rows) // N_SAMPLES, 1)
    batch = []
    for row in rows[::stride]:
        image = cv2.imread(row["path"], cv2.IMREAD_COLOR)
        if image is None:
            continue
        batch.append(cv2.resize(image, (input_size, input_size),
                                interpolation=cv2.INTER_LINEAR))
        if len(batch) == N_SAMPLES:
            break
    if len(batch) < N_SAMPLES:
        raise SystemExit(f"only {len(batch)} readable validation patches")
    stacked = np.stack(batch).transpose(0, 3, 1, 2)
    return np.ascontiguousarray(stacked, dtype=np.float32)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--onnx", type=Path, required=True)
    args = parser.parse_args()

    checkpoint = torch.load(args.checkpoint, map_location="cpu", weights_only=False)
    input_size = checkpoint["input_size"]

    backbone = mobilenet_v3_small(weights=None)
    backbone.classifier[3] = nn.Linear(backbone.classifier[3].in_features, 2)
    backbone.load_state_dict(checkpoint["backbone_state"])
    model = ServingModel(backbone, checkpoint["mean"], checkpoint["std"]).eval()

    patches = load_patches(input_size)
    with torch.no_grad():
        torch_out = model(torch.from_numpy(patches)).numpy()

    session = ort.InferenceSession(str(args.onnx), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    onnx_out = session.run(None, {input_name: patches})[0]

    delta = np.abs(torch_out - onnx_out)
    torch_labels = (torch_out >= 0.5).astype(int)
    onnx_labels = (onnx_out >= 0.5).astype(int)
    agreement = float((torch_labels == onnx_labels).mean())

    print(f"samples              {len(patches)}")
    print(f"input                {input_name} {patches.shape} float32 BGR [0,255]")
    print(f"max |delta|          {delta.max():.3e}  (limit {MAX_ABS_DELTA:.0e})")
    print(f"mean |delta|         {delta.mean():.3e}")
    print(f"label agreement      {agreement:.4f}")
    print(f"onnx P(occupied)     min {onnx_out.min():.4f} max {onnx_out.max():.4f} "
          f"mean {onnx_out.mean():.4f}")

    if delta.max() > MAX_ABS_DELTA or agreement < 1.0:
        print("\nFAIL: ONNX export does not match PyTorch. Do not benchmark this model.",
              file=sys.stderr)
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())