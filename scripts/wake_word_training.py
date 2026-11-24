"""Local custom-model preparation for the exact UNBOUND wake phrase.

This script intentionally accepts user-provided audio directories only. It
never downloads the ACAV100M feature set or a TTS voice, both of which need
separate license review for a possible commercial product.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
from typing import Any

PHRASE = "hey unbound"
AUDIO_SUFFIXES = {".wav", ".flac", ".ogg", ".opus", ".aiff", ".au", ".mp3", ".m4a"}
REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_DIR = REPO_ROOT / "backend" / "models" / "wake-word" / "features"
RUNTIME_MODEL = REPO_ROOT / "backend" / "models" / "wake-word" / "hey-unbound.onnx"


def _audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        raise ValueError(f"Audio folder does not exist: {folder}")
    files = sorted(path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES)
    if not files:
        raise ValueError(f"No supported audio recordings found in {folder}")
    return files


def _require_license_confirmation(args: argparse.Namespace) -> None:
    if not args.confirm_data_rights:
        raise ValueError(
            "Pass --confirm-data-rights only after confirming the recording/data licenses and participant consent. "
            "Do not use ACAV100M or other non-commercial-licensed data."
        )


def install_features(_: argparse.Namespace) -> int:
    try:
        from wakeword_trainer.paths import bundled_model_paths
    except ImportError as exc:
        raise RuntimeError("Install the isolated training environment first: scripts/wake-word.ps1 install-training") from exc
    mel, embedding = bundled_model_paths()
    FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(mel, FEATURE_DIR / "melspectrogram.onnx")
    shutil.copy2(embedding, FEATURE_DIR / "embedding_model.onnx")
    print(f"Copied the package feature models to {FEATURE_DIR}")
    print("No wake classifier, audio recordings, microphone, or external dataset was accessed.")
    print("Review docs/WAKE_WORD_MODEL_LICENSES.md before any commercial distribution.")
    return 0


def train(args: argparse.Namespace) -> int:
    _require_license_confirmation(args)
    positives = Path(args.positives).expanduser().resolve()
    negatives = Path(args.negatives).expanduser().resolve()
    positive_files = _audio_files(positives)
    negative_files = _audio_files(negatives)
    if positives == negatives or positives in negatives.parents or negatives in positives.parents:
        raise ValueError("Positive and negative recordings must be in separate, non-nested folders.")
    if len(positive_files) < args.minimum_samples:
        raise ValueError(f"Need at least {args.minimum_samples} positive recordings before training; found {len(positive_files)}.")
    if len(negative_files) < args.minimum_samples:
        raise ValueError(f"Need at least {args.minimum_samples} negative recordings before training; found {len(negative_files)}.")
    if any("acav100m" in str(path).lower() or "openwakeword_features" in str(path).lower() for path in negative_files):
        raise ValueError("ACAV100M-derived features are not permitted in this training path.")
    try:
        import wakeword_trainer as wwt
    except ImportError as exc:
        raise RuntimeError("Install the isolated training environment first: scripts/wake-word.ps1 install-training") from exc

    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    print(f"Training {PHRASE!r} on CPU from {len(positive_files)} positive and {len(negative_files)} negative audio files.")
    result = wwt.train(
        word=PHRASE,
        dataset=str(positives),
        output=str(output),
        preset=args.preset,
        negatives=str(negatives),
        download_negatives=False,
        export=True,
        export_tflite=False,
        device="cpu",
        target_fp_per_hour=args.target_fp_per_hour,
        progress=True,
    )
    candidate = output / "hey-unbound.onnx"
    shutil.copy2(result.onnx_path, candidate)
    summary: dict[str, Any] = result.summary()
    summary["unbound_candidate_onnx"] = str(candidate)
    report_path = output / "unbound_training_summary.json"
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Candidate model: {candidate}")
    print("This is a candidate only. Evaluate disjoint held-out audio and the target microphone before enabling it.")
    print(f"Training summary: {report_path}")
    return 0


def evaluate(args: argparse.Namespace) -> int:
    _require_license_confirmation(args)
    model_path = Path(args.model).expanduser().resolve()
    positives = Path(args.positives).expanduser().resolve()
    negatives = Path(args.negatives).expanduser().resolve()
    if not model_path.is_file() or model_path.suffix.lower() != ".onnx":
        raise ValueError("Evaluation needs an existing candidate .onnx classifier.")
    if not _audio_files(positives) or not _audio_files(negatives):
        raise ValueError("Evaluation needs separate held-out positive and negative audio.")
    try:
        import wakeword_trainer as wwt
    except ImportError as exc:
        raise RuntimeError("Install the isolated training environment first: scripts/wake-word.ps1 install-training") from exc
    result = wwt.evaluate(
        str(model_path),
        str(positives),
        split="all",
        negatives=str(negatives),
        download_negatives=False,
        target_fp_per_hour=args.target_fp_per_hour,
        output_report=True,
    )
    report = {
        "model": str(model_path),
        "phrase": PHRASE,
        "threshold": result.threshold,
        "metrics": result.metrics,
        "operating_point": result.operating_point,
        "report_dir": str(result.report_dir) if result.report_dir else None,
    }
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def install_model(args: argparse.Namespace) -> int:
    source = Path(args.model).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".onnx":
        raise ValueError("Provide a trained classifier .onnx file.")
    try:
        from wakeword_trainer import load_model_metadata
    except ImportError as exc:
        raise RuntimeError("Install the isolated training environment first: scripts/wake-word.ps1 install-training") from exc
    metadata = load_model_metadata(str(source))
    if " ".join(str(metadata.word).casefold().split()) != PHRASE:
        raise ValueError(f"Model phrase metadata must be exactly {PHRASE!r}; got {metadata.word!r}.")
    RUNTIME_MODEL.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, RUNTIME_MODEL)
    print(f"Installed candidate model at {RUNTIME_MODEL}")
    print(f"Model metadata threshold: {metadata.threshold}; runtime threshold is configured separately via UNBOUND_WAKE_THRESHOLD.")
    print("Run scripts/wake-word.ps1 validate. This verifies model loading only, not recognition accuracy.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train and evaluate the exact local Hey UNBOUND wake phrase.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install-features", help="Copy the Apache-noticed feature pipeline from the training package.").set_defaults(func=install_features)

    train_parser = commands.add_parser("train", help="Train from local positive and negative recordings on CPU.")
    train_parser.add_argument("--positives", required=True)
    train_parser.add_argument("--negatives", required=True)
    train_parser.add_argument("--output", default="generated_reports/wake-word/hey-unbound")
    train_parser.add_argument("--preset", choices=("quick", "balanced", "best"), default="balanced")
    train_parser.add_argument("--minimum-samples", type=int, default=50)
    train_parser.add_argument("--target-fp-per-hour", type=float, default=0.5)
    train_parser.add_argument("--confirm-data-rights", action="store_true")
    train_parser.set_defaults(func=train)

    eval_parser = commands.add_parser("evaluate", help="Evaluate on disjoint positive and negative recordings.")
    eval_parser.add_argument("--model", required=True)
    eval_parser.add_argument("--positives", required=True)
    eval_parser.add_argument("--negatives", required=True)
    eval_parser.add_argument("--target-fp-per-hour", type=float, default=0.5)
    eval_parser.add_argument("--confirm-data-rights", action="store_true")
    eval_parser.set_defaults(func=evaluate)

    install_parser = commands.add_parser("install-model", help="Copy a phrase-checked ONNX classifier into the ignored runtime model folder.")
    install_parser.add_argument("--model", required=True)
    install_parser.set_defaults(func=install_model)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Wake model operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
