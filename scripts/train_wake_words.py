#!/usr/bin/env python3
"""Train openWakeWord models for "Start Clerk" and "Over Clerk".

openWakeWord generates synthetic audio training data using text-to-speech
(no real recordings needed to start).  Run this script once on the server;
the resulting .onnx files are loaded by the /ws/wake-word WebSocket endpoint.

Usage
-----
    # Install training extras first (one-time):
    pip install "openwakeword[training]" pyttsx3 torch torchaudio torchinfo
    pip install torchmetrics lightning pronouncing audiomentations speechbrain mutagen

    # Then run:
    python scripts/train_wake_words.py

Output
------
    data/wake_word_models/start_clerk.onnx
    data/wake_word_models/over_clerk.onnx

After training, restart the FastAPI server — models are loaded on first
WebSocket connection.

Notes
-----
- Training takes ~15–30 minutes per phrase on CPU; ~5 min with GPU.
- The synthetic data pipeline uses gTTS / pyttsx3 for TTS — no internet
  needed if you use pyttsx3 (offline TTS).  gTTS requires internet; if
  the server is offline, set USE_OFFLINE_TTS=true below.
- Add real recordings of officers saying "Start Clerk" / "Over Clerk" into
    data/wake_word_training/start_clerk/positive/
    data/wake_word_training/over_clerk/positive/
  (.wav files, 16 kHz mono) to improve accuracy for Indian English accents.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Compatibility shim: torchaudio 2.x removed list_audio_backends() but
# speechbrain (a training dependency of openWakeWord) still calls it.
# ---------------------------------------------------------------------------
try:
    import torchaudio as _ta
    if not hasattr(_ta, "list_audio_backends"):
        _ta.list_audio_backends = lambda: ["soundfile", "sox_io"]
except ImportError:
    pass

import os
import sys
import logging
import pathlib

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PHRASES = {
    "start_clerk": "start clerk",
    "over_clerk":  "over clerk",
}
OUTPUT_DIR      = pathlib.Path("data/wake_word_models")
TRAINING_DIR    = pathlib.Path("data/wake_word_training")
N_SYNTH_SAMPLES = 50    # keep small so real recordings dominate (200:1 was killing recall)
USE_OFFLINE_TTS = True  # True → pyttsx3 (offline); False → gTTS (needs internet)
SAMPLE_RATE     = 16000
BATCH_SIZE      = 32
N_NEG_SAMPLES   = 300   # proportional to small positive set


def _check_deps():
    missing = []
    for pkg in ("openwakeword", "onnxruntime", "torch", "soundfile", "numpy"):
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            missing.append(pkg)
    if missing:
        log.error("Missing packages: %s", missing)
        log.error("Run:  pip install openwakeword[training] torch torchaudio soundfile")
        sys.exit(1)


def _generate_synthetic_samples(phrase: str, out_dir: pathlib.Path, n: int):
    """Generate N synthetic .wav files for `phrase` using TTS."""
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = list(out_dir.glob("*.wav"))
    if len(existing) >= n:
        log.info("  %d synthetic samples already exist — skipping TTS", len(existing))
        return

    log.info("  Generating %d synthetic TTS samples for '%s' ...", n, phrase)

    if USE_OFFLINE_TTS:
        try:
            import pyttsx3
            engine = pyttsx3.init()
            voices = engine.getProperty("voices")
            speeds = [130, 150, 170, 190]
            for i in range(n):
                engine.setProperty("rate", speeds[i % len(speeds)])
                if voices:
                    engine.setProperty("voice", voices[i % len(voices)].id)
                out_file = out_dir / f"synth_{i:04d}.wav"
                engine.save_to_file(phrase, str(out_file))
                engine.runAndWait()
            log.info("  Done — %d files in %s", n, out_dir)
        except ImportError:
            log.warning("pyttsx3 not installed; trying gTTS (needs internet)")
            _generate_with_gtts(phrase, out_dir, n)
        except Exception as exc:
            log.warning("pyttsx3 failed (%s); trying gTTS", exc)
            _generate_with_gtts(phrase, out_dir, n)
    else:
        _generate_with_gtts(phrase, out_dir, n)


def _generate_with_gtts(phrase: str, out_dir: pathlib.Path, n: int):
    try:
        from gtts import gTTS
        speeds = [False, True]
        for i in range(n):
            slow = speeds[i % 2]
            tts = gTTS(text=phrase, lang="en", slow=slow)
            out_file = out_dir / f"synth_{i:04d}.mp3"
            tts.save(str(out_file))
            if i % 50 == 0:
                log.info("    %d/%d ...", i, n)
        log.info("  Done — %d .mp3 files in %s", n, out_dir)
    except ImportError:
        log.error("Neither pyttsx3 nor gTTS available. Install: pip install pyttsx3")
        sys.exit(1)


def _load_clips(wav_dir: pathlib.Path, target_samples: int) -> list:
    """Load all wav files, resample to 16kHz, return list of int16 arrays (PCM)."""
    import numpy as np
    import soundfile as sf

    clips = []
    for wav_path in sorted(wav_dir.glob("*.wav")):
        try:
            data, sr = sf.read(str(wav_path), dtype="float32")
            if len(data.shape) > 1:
                data = data.mean(axis=1)           # stereo → mono
            if sr != SAMPLE_RATE:
                import scipy.signal as ss
                data = ss.resample_poly(data, SAMPLE_RATE, sr).astype("float32")
            # Pad / trim to target_samples
            if len(data) < target_samples:
                padded = np.zeros(target_samples, dtype=np.float32)
                padded[:len(data)] = data
                data = padded
            else:
                data = data[:target_samples]
            # Convert float32 → int16 (openWakeWord requires 16-bit PCM)
            clip_i16 = np.clip(data * 32768, -32768, 32767).astype(np.int16)
            clips.append(clip_i16)
        except Exception as exc:
            log.warning("  Could not load %s: %s", wav_path.name, exc)
    return clips


def _clip_generator(clips, batch_size=32):
    """Yield batches of shape (batch_size, samples) — int16 PCM."""
    import numpy as np
    for i in range(0, len(clips), batch_size):
        batch = clips[i:i + batch_size]
        yield np.stack(batch, axis=0)


def _noise_generator(n_total: int, clip_samples: int, batch_size: int = 32):
    """Yield batches of gaussian noise as int16 PCM negative examples."""
    import numpy as np
    produced = 0
    while produced < n_total:
        bs = min(batch_size, n_total - produced)
        r = np.random.random()
        if r < 0.5:
            batch = np.zeros((bs, clip_samples), dtype=np.int16)
        elif r < 0.75:
            batch = (np.random.randn(bs, clip_samples) * 1638).astype(np.int16)  # ~5% amplitude
        else:
            white = np.random.randn(bs, clip_samples)
            pink  = np.cumsum(white * 328, axis=1)            # ~1% amplitude pink
            batch = np.clip(pink, -32768, 32767).astype(np.int16)
        yield batch
        produced += bs


def _train_model(wake_word_name: str, phrase: str):
    """Train an openWakeWord model for a single wake word using Model.auto_train()."""
    import numpy as np
    from openwakeword.train import Model, AudioFeatures, compute_features_from_generator
    from numpy.lib.format import open_memmap

    log.info("=" * 60)
    log.info("Training model: %s  ('%s')", wake_word_name, phrase)
    log.info("=" * 60)

    pos_dir  = TRAINING_DIR / wake_word_name / "positive"
    out_path = OUTPUT_DIR / f"{wake_word_name}.onnx"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if out_path.exists():
        log.info("Model already exists at %s — skipping (delete to retrain)", out_path)
        return

    # 1. Generate synthetic positive samples
    _generate_synthetic_samples(phrase, pos_dir, N_SYNTH_SAMPLES)

    # 2. Determine clip length from AudioFeatures
    F = AudioFeatures(device="cpu")
    CLIP_SAMPLES = SAMPLE_RATE * 2       # 2-second clips
    feature_shape = F.get_embedding_shape(CLIP_SAMPLES / SAMPLE_RATE)
    log.info("  Feature shape per clip: %s", feature_shape)

    # 3. Load positive clips
    log.info("  Loading positive clips from %s ...", pos_dir)
    pos_clips = _load_clips(pos_dir, CLIP_SAMPLES)
    if not pos_clips:
        log.error("  No positive clips found in %s — aborting", pos_dir)
        sys.exit(1)
    log.info("  Loaded %d positive clips", len(pos_clips))

    # 4. Compute features for positive clips
    pos_feat_file = str(TRAINING_DIR / wake_word_name / "pos_features.npy")
    pathlib.Path(pos_feat_file).parent.mkdir(parents=True, exist_ok=True)
    if not pathlib.Path(pos_feat_file).exists():
        log.info("  Computing positive features → %s ...", pos_feat_file)
        gen = _clip_generator(pos_clips, batch_size=BATCH_SIZE)
        compute_features_from_generator(gen, len(pos_clips), CLIP_SAMPLES, pos_feat_file)
    X_pos = np.load(pos_feat_file)
    log.info("  Positive features: %s", X_pos.shape)

    # 5. Compute features for negative (noise) clips
    neg_feat_file = str(TRAINING_DIR / wake_word_name / "neg_features.npy")
    if not pathlib.Path(neg_feat_file).exists():
        log.info("  Computing negative (noise) features → %s ...", neg_feat_file)
        gen = _noise_generator(N_NEG_SAMPLES, CLIP_SAMPLES, batch_size=BATCH_SIZE)
        compute_features_from_generator(gen, N_NEG_SAMPLES, CLIP_SAMPLES, neg_feat_file)
    X_neg = np.load(neg_feat_file)
    log.info("  Negative features: %s", X_neg.shape)

    # 6. Build train / val splits
    import torch
    from torch.utils.data import TensorDataset, DataLoader
    from itertools import cycle

    n_pos      = len(X_pos)
    n_neg      = len(X_neg)
    n_val_pos  = max(1, n_pos // 5)          # 20 % of positives for val
    n_val_neg  = max(1, n_neg // 5)

    X_pos_train = X_pos[n_val_pos:]
    X_pos_val   = X_pos[:n_val_pos]
    X_neg_train = X_neg[n_val_neg:]
    X_neg_val   = X_neg[:n_val_neg]

    # Combine positive + negative for train/val
    X_tr = np.concatenate([X_pos_train, X_neg_train], axis=0)
    y_tr = np.concatenate([np.ones(len(X_pos_train)), np.zeros(len(X_neg_train))])
    X_vl = np.concatenate([X_pos_val, X_neg_val], axis=0)
    y_vl = np.concatenate([np.ones(len(X_pos_val)),  np.zeros(len(X_neg_val))])

    # shuffle train
    idx = np.random.permutation(len(X_tr))
    X_tr, y_tr = X_tr[idx], y_tr[idx]

    log.info("  Train set: %d pos + %d neg = %d total",
             len(X_pos_train), len(X_neg_train), len(X_tr))
    log.info("  Val set:   %d pos + %d neg = %d total",
             len(X_pos_val),   len(X_neg_val),   len(X_vl))

    # auto_train / train_model iterate over DataLoaders yielding (tensor, label).
    # With a small dataset we need a cycling loader so it doesn't exhaust before max_steps.
    STEPS = 10000  # increased for real + synthetic combined dataset
    BS    = 32

    def make_loader(X_arr, y_arr, batch_size=BS, shuffle=False):
        ds = TensorDataset(torch.FloatTensor(X_arr), torch.FloatTensor(y_arr))
        return DataLoader(ds, batch_size=batch_size, shuffle=shuffle)

    # Training loader cycles indefinitely (max_steps >> dataset size)
    train_loader  = cycle(make_loader(X_tr,    y_tr,    shuffle=True))
    # Val loaders must be FINITE — used once per eval pass
    val_loader    = make_loader(X_vl,    y_vl)
    fp_val_loader = make_loader(X_neg_val, np.zeros(len(X_neg_val)))

    # 7. Train the model
    # NOTE: auto_train ramps negative_weight from 1→1000 over all steps, which
    # destroys recall with small datasets (< 500 positives). Use train_model
    # directly with a constant weight of 1 so positives are never swamped.
    model = Model(input_shape=feature_shape)
    log.info("  Starting Model.train_model() (steps=%d, constant weight) ...", STEPS)
    val_steps = list(range(STEPS - STEPS // 4, STEPS, max(1, STEPS // 20)))
    model.train_model(
        X=train_loader,
        X_val=val_loader,
        false_positive_val_data=fp_val_loader,
        max_steps=STEPS,
        negative_weight_schedule=[1.0] * STEPS,   # constant — no ramp-up that kills recall
        val_steps=val_steps,
        warmup_steps=STEPS // 5,
        hold_steps=STEPS // 3,
        lr=0.0001,
        val_set_hrs=11.3,
    )
    # Pick the checkpoint with best recall
    if model.best_models:
        import copy
        best_idx = max(range(len(model.best_model_scores)),
                       key=lambda i: model.best_model_scores[i].get("val_recall", 0))
        model.model = copy.deepcopy(model.best_models[best_idx])
        s = model.best_model_scores[best_idx]
        log.info("  Best checkpoint — recall=%.3f  fp/hr=%.2f  accuracy=%.3f",
                 s.get("val_recall", 0), s.get("val_fp_per_hr", 0), s.get("val_accuracy", 0))

    # 8. Export to ONNX
    log.info("  Exporting model to %s ...", out_path)
    model.export_to_onnx(str(out_path), class_mapping=wake_word_name)
    log.info("  Model saved: %s", out_path)


def _verify_models():
    """Quick smoke-test: load both models and run inference on silence."""
    import numpy as np
    from openwakeword.model import Model as OWWModel

    models_found = [OUTPUT_DIR / f"{k}.onnx" for k in PHRASES if (OUTPUT_DIR / f"{k}.onnx").exists()]
    if not models_found:
        log.error("No models found in %s", OUTPUT_DIR)
        return

    log.info("Verifying models ...")
    model = OWWModel(wakeword_models=[str(p) for p in models_found], inference_framework="onnx")
    silence = np.zeros(1600, dtype=np.float32)
    preds = model.predict(silence)
    log.info("  Inference on silence: %s", {k: round(float(v), 4) for k, v in preds.items()})
    log.info("Models verified OK — restart FastAPI to load them.")


if __name__ == "__main__":
    _check_deps()

    for name, phrase in PHRASES.items():
        _train_model(name, phrase)

    _verify_models()

    log.info("")
    log.info("Done!  Models are in: %s", OUTPUT_DIR)
    log.info("Restart the FastAPI server to activate wake word detection.")
    log.info("")
    log.info("To improve accuracy with real Indian English recordings:")
    log.info("  1. Record officers saying 'Start Clerk' (20–50 samples)")
    log.info("  2. Save as 16 kHz mono .wav files in:")
    log.info("       data/wake_word_training/start_clerk/positive/")
    log.info("       data/wake_word_training/over_clerk/positive/")
    log.info("  3. Re-run this script (delete old .onnx files first)")
