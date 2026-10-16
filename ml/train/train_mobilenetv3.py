# ml/train/train_mobilenetv3.py
"""Method B — MobileNetV3-Small binary occupancy classifier. Runs on Colab.

Self-contained by design: this file is shipped inside the training package so
the Colab runtime needs nothing from the repo.

    python train_mobilenetv3.py --package-dir /content/pkg --out-dir /content/drive/MyDrive/apms

The exported ONNX model takes float32 NCHW BGR in [0,255] at 96x96 and returns
P(occupied) with shape (N,1). Channel reorder, scaling and normalisation happen
inside the graph, so the worker cannot preprocess differently from training.
Resize interpolation is INTER_LINEAR everywhere and is recorded in the model card.

Seeds are fixed and logged. cuDNN kernel selection remains nondeterministic, so
runs are reproducible in setup but not bitwise identical — do not claim otherwise.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler
from torchvision.models import MobileNet_V3_Small_Weights, mobilenet_v3_small

IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)
INTERPOLATION = cv2.INTER_LINEAR


# ----------------------------------------------------------------- data ----
class PatchDataset(Dataset):
    def __init__(self, root: Path, manifest: Path, input_size: int, train: bool):
        with open(manifest, newline="", encoding="utf-8") as handle:
            self.rows = list(csv.DictReader(handle))
        if not self.rows:
            raise SystemExit(f"{manifest} is empty")
        self.root = root
        self.input_size = input_size
        self.train = train
        self.labels = np.array([int(r["label"]) for r in self.rows], dtype=np.int64)

    def __len__(self) -> int:
        return len(self.rows)

    def _augment(self, image: np.ndarray, rng: random.Random) -> np.ndarray:
        if rng.random() < 0.5:
            image = cv2.flip(image, 1)
        angle = rng.uniform(-10.0, 10.0)
        if abs(angle) > 0.1:
            side = image.shape[0]
            matrix = cv2.getRotationMatrix2D((side / 2, side / 2), angle, 1.0)
            image = cv2.warpAffine(image, matrix, (side, side),
                                   flags=INTERPOLATION, borderMode=cv2.BORDER_REPLICATE)
        alpha = rng.uniform(0.8, 1.2)
        beta = rng.uniform(-25.0, 25.0)
        return np.clip(image.astype(np.float32) * alpha + beta, 0.0, 255.0)

    def __getitem__(self, index: int):
        row = self.rows[index]
        path = self.root / row["rel_path"]
        image = cv2.imread(str(path), cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError(f"unreadable patch: {path}")
        image = cv2.resize(image, (self.input_size, self.input_size),
                           interpolation=INTERPOLATION)
        if self.train:
            image = self._augment(image, random.Random(index ^ random.getrandbits(32)))
        else:
            image = image.astype(np.float32)
        tensor = torch.from_numpy(np.ascontiguousarray(image.transpose(2, 0, 1)))
        return tensor, int(row["label"])


# ---------------------------------------------------------------- model ----
class ServingModel(nn.Module):
    """Preprocessing is part of the graph. `logits` is used for training,
    `forward` is what gets exported — both share `_prep`, so there is no second
    preprocessing path that could drift."""

    def __init__(self, backbone: nn.Module):
        super().__init__()
        self.backbone = backbone
        self.register_buffer("bgr_to_rgb", torch.tensor([2, 1, 0], dtype=torch.long))
        self.register_buffer("mean", torch.tensor(IMAGENET_MEAN).view(1, 3, 1, 1) * 255.0)
        self.register_buffer("std", torch.tensor(IMAGENET_STD).view(1, 3, 1, 1) * 255.0)

    def _prep(self, x: torch.Tensor) -> torch.Tensor:
        x = x.index_select(1, self.bgr_to_rgb)
        return (x - self.mean) / self.std

    def logits(self, x: torch.Tensor) -> torch.Tensor:
        return self.backbone(self._prep(x))

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torch.softmax(self.logits(x), dim=1)[:, 1:2]


def build_model(pretrained: bool) -> ServingModel:
    weights = MobileNet_V3_Small_Weights.IMAGENET1K_V1 if pretrained else None
    backbone = mobilenet_v3_small(weights=weights)
    in_features = backbone.classifier[3].in_features
    backbone.classifier[3] = nn.Linear(in_features, 2)
    return ServingModel(backbone)


# ------------------------------------------------------------ evaluation ----
@torch.no_grad()
def evaluate(model: ServingModel, loader: DataLoader, device: str) -> dict:
    model.eval()
    tp = fp = tn = fn = 0
    for images, labels in loader:
        images = images.to(device, non_blocking=True)
        predictions = model.logits(images).argmax(dim=1).cpu().numpy()
        truths = labels.numpy()
        tp += int(((truths == 1) & (predictions == 1)).sum())
        fn += int(((truths == 1) & (predictions == 0)).sum())
        fp += int(((truths == 0) & (predictions == 1)).sum())
        tn += int(((truths == 0) & (predictions == 0)).sum())
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "accuracy": (tp + tn) / max(tp + fp + tn + fn, 1),
        "recall": recall,
        "specificity": specificity,
        "balanced_accuracy": (recall + specificity) / 2,
        "false_vacancy_rate": fn / (fn + tn) if fn + tn else None,
    }


# ---------------------------------------------------------------- export ----
def export_onnx(model: ServingModel, path: Path, input_size: int) -> str:
    model = model.to("cpu").eval()
    dummy = torch.randint(0, 256, (1, 3, input_size, input_size), dtype=torch.float32)
    kwargs = dict(
        opset_version=17,
        input_names=["patches_bgr_0_255"],
        output_names=["p_occupied"],
        dynamic_axes={"patches_bgr_0_255": {0: "batch"}, "p_occupied": {0: "batch"}},
    )
    try:
        torch.onnx.export(model, dummy, str(path), dynamo=False, **kwargs)
        return "torchscript"
    except TypeError:
        torch.onnx.export(model, dummy, str(path), **kwargs)
        return "default"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ------------------------------------------------------------------ main ----
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--input-size", type=int, default=96)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=1e-4)
    parser.add_argument("--patience", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20260101)
    parser.add_argument("--model-version", default=None)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    version = args.model_version or f"mnv3s-pklot-{time.strftime('%Y%m%d')}"
    args.out_dir.mkdir(parents=True, exist_ok=True)
    patches_root = args.package_dir / "patches"

    print(f"device={device}  version={version}  seed={args.seed}")
    if device == "cpu":
        print("WARNING: no GPU. Check Runtime > Change runtime type > T4 GPU.")

    train_set = PatchDataset(patches_root, args.package_dir / "manifests/same_lot_train.csv",
                             args.input_size, train=True)
    val_set = PatchDataset(patches_root, args.package_dir / "manifests/same_lot_val.csv",
                           args.input_size, train=False)

    counts = np.bincount(train_set.labels, minlength=2)
    print(f"train vacant={counts[0]:,} occupied={counts[1]:,}   val n={len(val_set):,}")

    # Class-balanced sampling, per 05_BENCHMARK_PROTOCOL.md. One epoch is
    # len(train_set) draws with replacement, so the minority class repeats.
    sample_weights = (1.0 / np.maximum(counts, 1))[train_set.labels]
    sampler = WeightedRandomSampler(
        torch.as_tensor(sample_weights, dtype=torch.double),
        num_samples=len(train_set), replacement=True,
        generator=torch.Generator().manual_seed(args.seed),
    )

    train_loader = DataLoader(train_set, batch_size=args.batch_size, sampler=sampler,
                              num_workers=args.workers, pin_memory=(device == "cuda"),
                              drop_last=True, persistent_workers=args.workers > 0)
    val_loader = DataLoader(val_set, batch_size=max(args.batch_size, 256), shuffle=False,
                            num_workers=args.workers, pin_memory=(device == "cuda"))

    model = build_model(pretrained=True).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr,
                                  weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.epochs)
    criterion = nn.CrossEntropyLoss()
    scaler = torch.amp.GradScaler(device, enabled=(device == "cuda"))

    best_score, best_metrics, bad_epochs, start_epoch = -1.0, {}, 0, 0
    history: list[dict] = []
    last_path = args.out_dir / "last.pt"
    best_path = args.out_dir / "best.pt"

    if args.resume and last_path.exists():
        state = torch.load(last_path, map_location=device, weights_only=False)
        model.load_state_dict(state["model_state"])
        optimizer.load_state_dict(state["optimizer_state"])
        scheduler.load_state_dict(state["scheduler_state"])
        start_epoch = state["epoch"] + 1
        best_score = state["best_score"]
        history = state["history"]
        print(f"resumed from epoch {start_epoch}")

    for epoch in range(start_epoch, args.epochs):
        model.train()
        running, seen, started = 0.0, 0, time.perf_counter()
        for step, (images, labels) in enumerate(train_loader, 1):
            images = images.to(device, non_blocking=True)
            labels = labels.to(device, non_blocking=True)
            optimizer.zero_grad(set_to_none=True)
            with torch.amp.autocast(device, enabled=(device == "cuda")):
                loss = criterion(model.logits(images), labels)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
            running += loss.item() * labels.size(0)
            seen += labels.size(0)
            if step % 50 == 0:
                print(f"\r  epoch {epoch} step {step}/{len(train_loader)} "
                      f"loss {running / seen:.4f}", end="", flush=True)
        scheduler.step()

        metrics = evaluate(model, val_loader, device)
        metrics.update(epoch=epoch, train_loss=running / max(seen, 1),
                       seconds=round(time.perf_counter() - started, 1))
        history.append(metrics)
        print(f"\r  epoch {epoch}  loss {metrics['train_loss']:.4f}  "
              f"val_acc {metrics['accuracy']:.4f}  "
              f"val_bal_acc {metrics['balanced_accuracy']:.4f}  "
              f"({metrics['seconds']}s)")

        torch.save({"epoch": epoch, "model_state": model.state_dict(),
                    "optimizer_state": optimizer.state_dict(),
                    "scheduler_state": scheduler.state_dict(),
                    "best_score": best_score, "history": history}, last_path)

        if metrics["balanced_accuracy"] > best_score:
            best_score, best_metrics, bad_epochs = metrics["balanced_accuracy"], metrics, 0
            torch.save({"backbone_state": model.backbone.state_dict(),
                        "arch": "mobilenet_v3_small", "input_size": args.input_size,
                        "mean": IMAGENET_MEAN, "std": IMAGENET_STD,
                        "channel_order": "BGR", "value_range": "0-255",
                        "epoch": epoch, "val_metrics": metrics}, best_path)
            print(f"    new best (balanced accuracy {best_score:.4f}) -> {best_path.name}")
        else:
            bad_epochs += 1
            if bad_epochs >= args.patience:
                print(f"  early stop: no improvement in {args.patience} epochs")
                break

    print("\nExporting ONNX...")
    checkpoint = torch.load(best_path, map_location="cpu", weights_only=False)
    export_model = build_model(pretrained=False)
    export_model.backbone.load_state_dict(checkpoint["backbone_state"])
    onnx_path = args.out_dir / f"{version}.onnx"
    exporter = export_onnx(export_model, onnx_path, args.input_size)

    card = {
        "version": version,
        "arch": "mobilenet_v3_small",
        "onnx_sha256": sha256_of(onnx_path),
        "onnx_bytes": onnx_path.stat().st_size,
        "onnx_opset": 17,
        "onnx_exporter": exporter,
        "input": {
            "name": "patches_bgr_0_255",
            "shape": ["batch", 3, args.input_size, args.input_size],
            "dtype": "float32",
            "channel_order": "BGR",
            "value_range": "0-255",
            "resize_interpolation": "cv2.INTER_LINEAR",
            "normalisation": "inside the graph (ImageNet mean/std)",
        },
        "output": {"name": "p_occupied", "shape": ["batch", 1],
                   "meaning": "probability the slot is OCCUPIED"},
        "training": {
            "seed": args.seed, "epochs_requested": args.epochs,
            "epochs_run": len(history), "batch_size": args.batch_size,
            "lr": args.lr, "weight_decay": args.weight_decay,
            "optimizer": "AdamW", "scheduler": "CosineAnnealingLR",
            "sampling": "WeightedRandomSampler, inverse class frequency",
            "augmentation": "hflip p=0.5; rotation +/-10deg; brightness/contrast",
            "selection_metric": "val balanced accuracy",
            "train_vacant": int(counts[0]), "train_occupied": int(counts[1]),
            "val_n": len(val_set),
        },
        "best_val_metrics": best_metrics,
        "history": history,
        "reproducibility_note": (
            "Seeds are fixed and logged. cuDNN kernel selection is not forced to "
            "be deterministic, so repeated runs are close but not bitwise identical."
        ),
    }
    package_json = args.package_dir / "PACKAGE.json"
    if package_json.exists():
        card["package"] = json.loads(package_json.read_text())

    card_path = args.out_dir / f"{version}.json"
    card_path.write_text(json.dumps(card, indent=2), encoding="utf-8")

    print(f"  {onnx_path}  ({card['onnx_bytes'] / 1e6:.1f} MB, exporter={exporter})")
    print(f"  sha256 {card['onnx_sha256']}")
    print(f"  {card_path}")

    # Parity check here, while the PyTorch model is still in memory. Repeat it
    # locally against the downloaded file — different runtime, different CPU.
    import onnxruntime as ort

    session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
    probe = torch.randint(0, 256, (16, 3, args.input_size, args.input_size),
                          dtype=torch.float32)
    with torch.no_grad():
        torch_out = export_model.eval()(probe).numpy()
    onnx_out = session.run(None, {"patches_bgr_0_255": probe.numpy()})[0]
    delta = float(np.abs(torch_out - onnx_out).max())
    print(f"  in-notebook parity max|delta| = {delta:.3e}")
    if delta > 1e-4:
        print("  FAIL: export diverges from PyTorch. Do not use this model.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())