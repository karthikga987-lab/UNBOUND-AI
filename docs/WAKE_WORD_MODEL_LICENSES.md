# Hey UNBOUND Model and Dataset Licensing Notes

This is an engineering inventory, not legal advice. Do not market or distribute the wake model commercially until the feature-model license discrepancy below has been resolved.

## Selected workflow

- Runtime framework: [openWakeWord](https://github.com/dscripka/openWakeWord), Apache-2.0 code.
- Trainer: [`wakeword-trainer` 0.1.2](https://pypi.org/project/wakeword-trainer/), whose package metadata and source declare Apache-2.0. PyPI currently labels it Alpha, shows classifiers only through Python 3.12, and has no publication provenance for the wheel. The package was installed and imported in an isolated Python 3.14.4 environment here, but its Alpha status remains a maintenance/supply-chain risk.
- Classifier: custom ONNX produced by the trainer from local recordings. The trainer's export docs specify the openWakeWord-compatible `(batch, frames, 96)` input and `(batch, 1)` output contract. The ONNX export is not itself a grant of rights over its training inputs.
- Runtime feature models: the trainer package's `NOTICE` says its bundled `melspectrogram.onnx` and `embedding_model.onnx` originate from openWakeWord and Google speech_embedding and are redistributed under Apache-2.0. However, upstream openWakeWord's README broadly says its included pretrained models are CC-BY-NC-SA-4.0 due to training data. Those statements may refer to different model subsets, but the exact relationship has not been legally confirmed. The setup copies only the trainer package's two named feature assets; their commercial suitability remains **unresolved** until the upstream asset provenance and licenses are reconciled.

## Training data policy

- Do not download/use `openwakeword_features_ACAV100M_2000_hrs_16bit.npy`, ACAV100M, or any other CC-BY-NC-SA data. The ACAV-derived openWakeWord feature dataset is explicitly marked CC-BY-NC-SA-4.0. Its 17 GB size is not a reason to use it.
- The provided scripts use only separate user-provided positive and negative audio directories and set `download_negatives=False`. They do not fetch public negative corpora, TTS voices, or datasets.
- Prefer recordings created by the team or participants who gave consent, with room noise, ordinary speech, and explicit hard-negative phrases. Keep provenance and permissions for every contributor. Avoid recording third-party copyrighted media without permission.
- The trainer's dataset/report files preserve splits and metrics. The user remains responsible for rights to each recording. The generated weights may embody information from those recordings; do not redistribute them unless the relevant permissions allow it.
- No TTS voice model is used by this workflow. If synthetic samples are added later, record the exact voice/model version and its license. LibriTTS-R and some Piper voice cards are CC-BY-4.0, which requires attribution; this is not currently part of the implemented pipeline.

## Sources checked

- [openWakeWord license and model notice](https://github.com/dscripka/openWakeWord/blob/main/README.md)
- [openWakeWord custom training guide](https://github.com/dscripka/openWakeWord#training-new-models)
- [ACAV-derived feature dataset card](https://huggingface.co/datasets/davidscripka/openwakeword_features)
- [`wakeword-trainer` PyPI metadata](https://pypi.org/project/wakeword-trainer/)
- [`wakeword-trainer` NOTICE](https://github.com/DUK-Codes/wakeword-trainer/blob/main/NOTICE)
- [`wakeword-trainer` ONNX contract](https://github.com/DUK-Codes/wakeword-trainer/blob/main/docs/export.md)
- [Piper voice model collection](https://huggingface.co/rhasspy/piper-voices)

## Release gate

Before a commercial release, obtain review of the two feature-model files' exact provenance and the upstream-vs-trainer license statements. Also audit the pinned trainer wheel and verify source/wheel correspondence or build from an audited source revision. Until then, classify the detector as local prototype code with license review pending; do not claim commercial clearance.
